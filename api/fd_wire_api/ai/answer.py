"""问答轨：不依赖数据处理引擎的自然语言助手（wire-assistant-panel）。

与构建轨（``loop.run_build_loop``）的关系是**并列而非分叉**：

- 构建轨要 cell、要工具、会写产物草稿——免费档没有 cell，在调 LLM 之前就
  被 ``CELL_NOT_PROVISIONED_MESSAGE`` 短路。
- 本模块**不碰 ToolExecutor、不解析 cell 端点、不写产物**——问答轨的全部
  产出就是一段自然语言。工具列表恒为空（``tools=[]``）是结构性保证：模型
  即便想调工具也无工具可调，无需靠运行时判断兜底。

纪律沿用既有循环：
- LLM 异常如实上抛（调用方转成对客可见的失败文案），绝不编造回答。
- 上下文注入只含标识与元数据（见 :func:`assistant_context_block`），
  绝不带数据行或快照值。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..artifacts.service import ArtifactNotFound, get_customer_artifact
from ..datasets.models import Dataset
from .protocols import LlmAssistantMessage, LlmClient

#: 浮层可注入的上下文字段白名单——多余键一律丢弃（不透传客户端任意载荷）。
CONTEXT_KEYS: tuple[str, ...] = ("tab", "artifact_id", "dataset_id")

#: 页签名 → 中文标签（指路用；未知页签回落原值）。
_TAB_LABELS: dict[str, str] = {
    "workbench": "工作台",
    "chat": "AI 构建",
    "artifacts": "产物",
    "datasets": "我的数据",
    "entities": "实体",
    "packs": "源包",
    "demands": "我的需求",
    "usage": "用量",
    "keys": "数据面 Key",
    "team": "团队",
    "market": "数据市场",
}

#: 页面能力表（指路事实来源）：页签名 → 一句话说明。仅描述**界面能做什么**，
#: 不承诺任何数据可得性——数据是否可得由客户自己的数据集与档位决定。
_TAB_CAPABILITIES: dict[str, str] = {
    "workbench": "聚合各页的信息资产，含置顶看板与最近产物入口",
    "chat": "用自然语言对话构建图表、指标、报告、预警与刷新计划",
    "artifacts": "查看与管理已生成的产物，可设可见性、发布到市场、分享链接",
    "datasets": "上传数据、接连接器、看字段与质量规则、设可见性",
    "entities": "声明业务实体与实体间关系，供 AI 理解列的业务含义",
    "packs": "订阅主题数据源包",
    "demands": "查看已提交的数据需求单与进展",
    "usage": "查看调用量、账期与计量明细",
    "keys": "管理 REST 数据面访问凭据",
    "team": "管理成员、席位与邀请",
    "market": "浏览其他企业开放的数据集与看板，按需订阅",
}

SYSTEM_PROMPT = (
    "你是柏讯控制台的助手。客户在控制台里就当前页面提问，你负责解释与指路。"
    "你的职责范围：说明某个页签能做什么、某个功能在哪里、某个术语或口径是什么"
    "意思、客户想达成的目标应该去哪个页面。"
    "纪律："
    "① 你**不能**执行任何操作、不能取数、不能生成产物——需要动手时，明确告诉"
    "客户去哪个页签、点哪个入口。"
    "② 只说你知道的。涉及具体数据值时不要猜测或编造，请客户去对应页面查看。"
    "③ 回答简洁，用中文，通常两三句话；指路时给出页签名。"
    "④ 若客户的问题超出控制台范围（例如问某只股票明天涨不涨），如实说明这不在"
    "你的职责内。"
)


def sanitize_context(raw: Any) -> dict[str, Any]:
    """客户端上下文 → 白名单净化后的 dict（未知键丢弃，类型不符丢弃）。

    只保留 :data:`CONTEXT_KEYS` 里的键；``tab`` 必须是字符串，
    ``artifact_id`` / ``dataset_id`` 必须是正整数。其余一律忽略——
    上下文是锦上添花，坏值不该阻断主流程。
    """
    if not isinstance(raw, dict):
        return {}
    clean: dict[str, Any] = {}
    tab = raw.get("tab")
    if isinstance(tab, str) and tab.strip():
        clean["tab"] = tab.strip()
    for key in ("artifact_id", "dataset_id"):
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            clean[key] = value
        elif isinstance(value, str) and value.isdigit() and int(value) > 0:
            clean[key] = int(value)
    return clean


def assistant_context_block(
    db: Session,
    datasets_db: Session,
    *,
    customer_id: str,
    context: dict[str, Any],
) -> str | None:
    """上下文 → system 提示追加块；无可注入内容 → None（零注入零漂移）。

    两库分设：``db`` 是 artifacts/AI 库会话（产物在 ai 库），``datasets_db``
    是 datasets 库会话——生产上这是两个库，混用会 ``no such table``。
    归属校验失败的对象**静默丢弃**——不报错、不暴露「该 id 存在但不属于你」。
    注入内容严格限于标识与标题类元数据：产物只取 kind/title/面板数，数据集
    只取 name/key/行数，**绝不读 spec 内容、快照值或任何数据行**。
    """
    lines: list[str] = []

    tab = context.get("tab")
    if isinstance(tab, str):
        label = _TAB_LABELS.get(tab, tab)
        capability = _TAB_CAPABILITIES.get(tab)
        if capability:
            lines.append(f"客户当前在「{label}」页（{capability}）。")
        else:
            lines.append(f"客户当前在「{label}」页。")

    artifact_id = context.get("artifact_id")
    if isinstance(artifact_id, int):
        try:
            artifact = get_customer_artifact(
                db, artifact_id=artifact_id, customer_id=customer_id
            )
        except ArtifactNotFound:
            artifact = None  # 他企/不存在：静默丢弃该项
        if artifact is not None:
            detail = f"客户正在查看产物「{artifact.title}」（类型 {artifact.kind}）"
            spec = _artifact_spec_for_summary(db, artifact)
            panels = spec.get("panels") if isinstance(spec, dict) else None
            if isinstance(panels, list):
                detail += f"，共 {len(panels)} 个面板"
            lines.append(detail + "。")

    dataset_id = context.get("dataset_id")
    if isinstance(dataset_id, int):
        dataset = datasets_db.get(Dataset, str(dataset_id))
        # 归属校验：非本企业等同不存在（静默丢弃）。
        if dataset is not None and dataset.customer_id == customer_id:
            lines.append(
                f"客户正在查看数据集「{dataset.name}」（标识 {dataset.key}，"
                f"约 {dataset.row_count} 行）。"
            )

    if not lines:
        return None
    return "【当前页面上下文】" + "\n".join(lines)


def _artifact_spec_for_summary(db: Session, artifact: Any) -> dict:
    """取当前版本 spec 用于「面板数」这类计数——只读结构，不读快照值。"""
    from ..artifacts.service import current_spec

    try:
        return current_spec(db, artifact)
    except Exception:  # noqa: BLE001 - 摘要失败不该阻断问答
        return {}


def answer_question(
    *,
    history: list[dict],
    llm: LlmClient,
    system_extra: str | None = None,
) -> LlmAssistantMessage:
    """一次问答补全：``tools=[]``（结构性保证：问答轨无工具可调）。

    ``history`` 是 OpenAI 形态的消息列表（不含 system 行——本函数负责拼）。
    ``system_extra`` 为上下文块；None 时 system 文本与固定文案逐字节一致
    （零漂移，与 ``loop._wire_history`` 同款纪律）。
    """
    system_content = SYSTEM_PROMPT
    if system_extra:
        system_content = f"{SYSTEM_PROMPT}\n{system_extra}"
    return llm.complete([{"role": "system", "content": system_content}, *history], [])


def answer_history(db: Session, conversation_id: int) -> list[dict]:
    """会话消息 → 问答轨 wire 历史（只有 user / assistant 两种角色）。

    与 ``loop._wire_history`` 的区别是**刻意**的：问答轨不回放 tool_trace——
    那些是构建轨的留痕，回放会让模型以为自己能调工具，也白白撑大上下文。
    同一会话里若曾发生过构建，其 assistant 文本仍按普通历史保留（客户看得到
    对话连续性）；工具留痕只被跳过，不报错。
    """
    from sqlalchemy import select

    from .models import AIMessage, MessageRole

    rows = list(
        db.scalars(
            select(AIMessage)
            .where(AIMessage.conversation_id == conversation_id)
            .order_by(AIMessage.id)
        ).all()
    )
    wire: list[dict] = []
    for row in rows:
        if row.role == MessageRole.user.value:
            wire.append({"role": "user", "content": row.content})
        elif row.role == MessageRole.assistant.value and row.content.strip():
            # 纯工具调用轮 content 为空：跳过，避免发空消息给兼容面。
            wire.append({"role": "assistant", "content": row.content})
    return wire
