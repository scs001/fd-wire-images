"""看板无头刷新轮（wire-dashboard-refresh 任务 1.1）。

复用既有 AI 构建链路跑一轮「取数刷新」，但不落会话表——**无头**：不建
conversation、不写 ai_messages，整个循环在内存 wire 里完成：

``系统指令（当前完整 spec JSON + 刷新要求）→ LLM 按各面板 source 自选
数据工具取数（经 ToolExecutor 打到 cell MCP，平台凭据轨道）→ 终答 =
更新后的完整 spec JSON``，取回的 spec 经最小校验后由端点走 PUT 语义
落新版本。轮内**零落盘**——LLM/引擎/解析任何一步失败都不产生半写版本。

复用与绕开的边界（proposal：loop 强耦合会话模型则抽最小可复用段）：
- **复用**：``LlmClient`` / ``ToolExecutor`` 协议与生产实现（llm/executor，
  依赖注入照 ai 路由）、``TOOL_SCHEMAS``、loop 的 ``_assistant_wire``
  （assistant → OpenAI wire 归一化，唯一抽出的可复用段）、cell 未开通
  护栏（``resolve_cell_endpoint`` / ``CellNotProvisionedError`` 语义）。
- **绕开**：``run_build_loop`` 本体（与 conversation/AIMessage 持久化强
  耦合，刷新不该在会话表留痕）、五类产物工具的暂存语义——刷新轮**禁写
  草稿**，LLM 误调产物写入工具时回灌错误结果，不 stage。
"""

from __future__ import annotations

import copy
import json
import threading

from ..ai.executor import CellNotProvisionedError, resolve_cell_endpoint
from ..ai.loop import _assistant_wire
from ..ai.protocols import LlmClient, ToolCall, ToolExecutor
from ..ai.tools import ARTIFACT_TOOL_KINDS, TOOL_SCHEMAS
from .service import validate_spec


class RefreshFailed(RuntimeError):
    """无头刷新轮失败（LLM 网关异常 / 超轮次无终答 / 终答解析或校验不过）。

    端点转 502 带原因——不落任何版本（失败零半写）。
    """


def refresh_system_prompt(current_spec: dict) -> str:
    """无头刷新轮系统指令：刷新要求 + 当前完整 spec JSON。

    面板结构与 id 不动、只更新 snapshot、终答仅输出更新后完整 spec 的
    JSON——这些约束都写进指令（LLM 侧口径）；服务端另有最小校验兜底。
    """
    return (
        "你是柏讯控制台的看板数据刷新执行器（无头模式，无客户对话）。"
        "下面给出当前仪表盘的完整 spec JSON。任务：按每个面板的 source"
        "（指标 code 或数据集名）逐一面板重新取数——用可用的数据查询工具"
        "获取最新数据，把结果写进该面板 snapshot（columns/rows 口径与原 "
        "spec 相同，kpi 数值放 rows[0][0]）。面板结构、面板 id、type、"
        "span、title、source、config 一律保持不动，只更新 snapshot；"
        "**面板的 encoding、panes、overlays、height、raw_option 等高级"
        "字段同样一律原样保留**——它们描述图表的呈现方式而非数据，改动会"
        "让客户丢失自己配的轴类型、副图与阈值线。带 raw_option 的面板其"
        "数据自含于该配置，跳过不刷新（原样保留）。"
        "某面板取不到新数据时保留原 snapshot。禁止调用任何产物写入工具。"
        "全部取数完成后，终答仅输出更新后的完整 spec JSON（一个 JSON "
        "对象，结构与输入相同），不要输出任何解释或 JSON 以外的文本。"
        "\n\n当前 spec：\n" + json.dumps(current_spec, ensure_ascii=False)
    )


#: 触发刷新的用户消息（与系统指令同口径，双保险提醒 LLM 输出纯 JSON）
_REFRESH_INSTRUCTION = (
    "执行刷新：按各面板 source 重新取数并更新 snapshot，"
    "然后仅输出更新后的完整 spec JSON。"
)


def _loads_object(candidate: str) -> dict | None:
    """尝试把文本解析为 JSON 对象；非对象或解析失败返回 None。"""
    try:
        parsed = json.loads(candidate)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def extract_spec_output(text: str) -> dict:
    """从容错终答提取 spec JSON 对象。

    解析序：原文直接解析 → 剥 `````json`` 围栏 → 首个 ``{`` 到末个 ``}``
    截取；全部失败抛 :class:`RefreshFailed`（带终答片段，端点 502 呈现）。
    """
    raw = (text or "").strip()
    candidates = [raw]
    if raw.startswith("```"):
        # 剥围栏：```json\n{...}\n``` → {...}（语言标记行一并去掉；
        # 围栏内容取最长段，容忍围栏外的前后寒暄文本）。
        chunks = [c for c in raw.split("```") if c.strip()]
        if chunks:
            inner = max(chunks, key=len).strip()
            if inner[:4].lower() == "json":
                inner = inner[4:].lstrip()
            candidates.append(inner)
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        candidates.append(raw[start : end + 1])
    for candidate in candidates:
        parsed = _loads_object(candidate)
        if parsed is not None:
            return parsed
    raise RefreshFailed(
        "刷新轮终答无法解析为 JSON 对象（期望更新后的完整 spec JSON）——"
        f"终答片段：{(text or '')[:200]!r}"
    )


def _execute_refresh_tool(call: ToolCall, tool_executor: ToolExecutor) -> dict:
    """刷新轮工具执行：数据工具打到 cell executor；产物写入工具一律拒绝。

    与 loop.execute_tool_call 同口径的失败语义：``CellNotProvisionedError``
    穿透上抛（端点 403 档位提示），其余异常转错误结果回灌 LLM 继续。
    """
    if call.name in ARTIFACT_TOOL_KINDS:
        return {
            "ok": False,
            "error": (
                f"刷新轮禁用产物写入工具 {call.name}——请继续用数据工具取数，"
                "完成后直接在终答里输出更新后的完整 spec JSON"
            ),
        }
    try:
        return tool_executor.execute(call.name, call.arguments)
    except CellNotProvisionedError:
        raise  # 未开通：穿透到端点走档位提示（约定派生口径的握手期发现）
    except Exception as exc:
        return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}


def run_refresh_round(
    *,
    current_spec: dict,
    llm: LlmClient,
    tool_executor: ToolExecutor,
    customer_id: str,
    max_turns: int,
) -> str:
    """跑一轮无头刷新循环，返回 LLM 终答文本（不落库、不 stage 草稿）。

    护栏与 loop 同款：cell 未开通（显式 map 未命中）→ 立即抛
    ``CellNotProvisionedError``，零 LLM/零工具调用；LLM 网关异常 →
    ``RefreshFailed`` 带原因；达轮次上限仍无终答 → ``RefreshFailed``。
    """
    if resolve_cell_endpoint(customer_id) is None:
        raise CellNotProvisionedError(
            f"customer {customer_id} 的 cell 未开通（无 MCP 端点）"
        )
    wire: list[dict] = [
        {"role": "system", "content": refresh_system_prompt(current_spec)},
        {"role": "user", "content": _REFRESH_INSTRUCTION},
    ]
    for _ in range(max_turns):
        try:
            assistant = llm.complete(wire, TOOL_SCHEMAS)
        except CellNotProvisionedError:
            raise
        except Exception as exc:
            raise RefreshFailed(f"AI 服务调用失败，刷新已安全中止：{exc}") from exc
        if not assistant.has_tool_calls:
            return assistant.text()  # 终答 = 更新后的完整 spec JSON 文本
        wire.append(_assistant_wire(assistant))
        for call in assistant.tool_calls:
            result = _execute_refresh_tool(call, tool_executor)
            wire.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )
    raise RefreshFailed(
        f"刷新轮达到轮次上限（AI_MAX_TURNS={max_turns}）仍未给出更新后的 "
        "spec 终答，已安全中止，未产生任何新版本"
    )


def refresh_dashboard_spec(
    *,
    current_spec: dict,
    llm: LlmClient,
    tool_executor: ToolExecutor,
    customer_id: str,
    max_turns: int,
) -> dict:
    """无头刷新全流程：跑轮 → 解析终答 JSON → 最小校验 → **结构归位**。

    ``CellNotProvisionedError`` 原样穿透（端点 403 档位提示）；解析/校验
    失败抛 :class:`RefreshFailed`（端点 502 带原因，不落版本）。
    """
    final_text = run_refresh_round(
        current_spec=current_spec,
        llm=llm,
        tool_executor=tool_executor,
        customer_id=customer_id,
        max_turns=max_turns,
    )
    spec = extract_spec_output(final_text)
    error = validate_spec(spec)
    if error is not None:
        raise RefreshFailed(f"刷新输出的 spec 未过最小校验：{error}")
    return restore_panel_structure(current_spec, spec)


#: 刷新只允许改 snapshot；这些面板字段是"呈现方式"，LLM 改了就归位。
_PRESERVED_PANEL_FIELDS: tuple[str, ...] = (
    "id",
    "type",
    "title",
    "span",
    "source",
    "config",
    "query",
    "encoding",
    "panes",
    "overlays",
    "height",
    "raw_option",
)


def _panel_entries(spec: object) -> list | None:
    """spec（信封/本体两种形状）→ 面板数组；无面板数组返回 None。

    就地实现而**不**从 query_contract 导入：query_contract 依赖本模块的
    ``RefreshFailed``，反向导入会形成循环（两者共享同一宽容口径，此处保持
    逐字一致）。
    """
    if not isinstance(spec, dict):
        return None
    inner = spec.get("spec")
    if isinstance(inner, dict) and isinstance(inner.get("panels"), list):
        return inner["panels"]
    if isinstance(spec.get("panels"), list):
        return spec["panels"]
    return None


def restore_panel_structure(current_spec: dict, refreshed_spec: dict) -> dict:
    """把 LLM 输出里被改动/丢弃的面板结构字段按原 spec 归位（wire-chart-contract-v2）。

    指令已要求"只更新 snapshot"，但 LLM 输出不可信（可能省略它认为多余的
    字段、或误改轴类型）。这里做**服务端兜底**：

    - 按面板 id（缺 id 时按原顺序索引）配对；
    - 配对成功：除 snapshot 外的所有字段一律取原 spec 的值；
    - **带 raw_option 的面板整体保留原样**（含 snapshot——其数据自含于
      配置，刷新无意义），即 kept；
    - 配对不上（LLM 增删面板）：原面板整体补回，不改看板结构。

    结果：刷新只能改数据，改不动结构——与 spec「v2 表达力不被刷新抹平」
    和「raw_option 面板 kept」两条要求一致。
    """
    current_panels = _panel_entries(current_spec)
    refreshed_panels = _panel_entries(refreshed_spec)
    if current_panels is None:
        return refreshed_spec
    if refreshed_panels is None:
        # LLM 把 panels 丢了：整份回退，不产生结构损毁的新版本。
        return copy.deepcopy(current_spec)

    # 按 id 建索引（有 id 用 id；无 id 用序号回退键）。
    def key_of(panel: object, index: int) -> str:
        if isinstance(panel, dict):
            value = panel.get("id")
            if isinstance(value, str) and value:
                return f"id:{value}"
        return f"idx:{index}"

    refreshed_by_key = {
        key_of(panel, index): panel for index, panel in enumerate(refreshed_panels)
    }

    merged: list[object] = []
    for index, current in enumerate(current_panels):
        if not isinstance(current, dict):
            merged.append(current)
            continue
        candidate = refreshed_by_key.get(key_of(current, index))
        # raw_option 面板：整块原样保留（数据自含，契约无从回写）。
        if current.get("raw_option") is not None:
            merged.append(copy.deepcopy(current))
            continue
        if not isinstance(candidate, dict):
            merged.append(copy.deepcopy(current))
            continue
        next_panel = copy.deepcopy(candidate)
        for field in _PRESERVED_PANEL_FIELDS:
            if field == "id":
                continue
            if field in current:
                next_panel[field] = copy.deepcopy(current[field])
            else:
                next_panel.pop(field, None)
        # id 也用原值（防止 LLM 改了 id 打乱后续配对）
        next_panel["id"] = current.get("id", next_panel.get("id"))
        merged.append(next_panel)

    result = copy.deepcopy(refreshed_spec)
    result["spec"]["panels"] = merged
    return result


# ------------------------------------------------------------ 单产物进程内锁
# v1 口径：module 级 dict 标记「artifact_id → 刷新进行中」，try/finally 由
# 端点保证释放；多副本部署下各进程各自单飞（跨进程互斥属 v2 调度器范畴）。
_refresh_in_flight: dict[int, bool] = {}
_in_flight_guard = threading.Lock()


def acquire_refresh_lock(artifact_id: int) -> bool:
    """尝试占住单产物刷新锁；已被占（进行中）返回 False（端点转 409 单飞）。"""
    with _in_flight_guard:
        if _refresh_in_flight.get(artifact_id):
            return False
        _refresh_in_flight[artifact_id] = True
        return True


def release_refresh_lock(artifact_id: int) -> None:
    """释放单产物刷新锁（幂等；端点 finally 段调用）。"""
    with _in_flight_guard:
        _refresh_in_flight.pop(artifact_id, None)
