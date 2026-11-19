"""demands API 的请求/响应模型（pydantic v2）。"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .orchestration_sync import orchestration_customer_view
from .source_policy import BLOCKED_SOURCE_REASON, blocked_source_domain
from .statemachine import DemandStatus

#: 联系邮箱的最小形态校验（非空 + 单 @ + 两侧各有非空白字符；不追求 RFC 全集）。
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _public_source_url(value: str) -> str:
    """单条数据源地址的合规门硬校验（wire-demand-generation-orchestration 1.2）。

    - 仅 http:// 或 https://（大小写不敏感）；
    - 不得内嵌账密（netloc 含 ``@`` 的 user:pass@host 形态即拒——工单面
      只承接公开数据，凭据形态地址直接挡在提交口）；
    - 平台禁爬清单（wire-crawl-source-blocklist）：淘宝/拼多多系域名
      （含子域）即拒——用户定规 2026-10-12，代爬服务面按平台级禁区硬拒。

    挂在列表元素上校验：第 i 条非法 → 错误 loc 带 (source_urls, i) 索引定位。
    """
    stripped = value.strip()
    if not stripped:
        raise ValueError("数据源地址不能为空")
    lowered = stripped.lower()
    if not lowered.startswith(("http://", "https://")):
        raise ValueError(
            "数据源地址必须以 http:// 或 https:// 开头（公开网页地址形态）"
        )
    try:
        netloc = urlsplit(stripped).netloc
    except ValueError:
        raise ValueError("数据源地址格式不合法") from None
    if "@" in netloc:
        raise ValueError(
            "数据源地址不得内嵌账号密码（user:pass@host 形态）——仅承接公开数据"
        )
    blocked = blocked_source_domain(stripped)
    if blocked is not None:
        raise ValueError(f"数据源命中禁爬平台 {blocked}——{BLOCKED_SOURCE_REASON}")
    return stripped


#: 合规门校验后的公开源地址元素类型（错误逐条带索引）。
SourceUrl = Annotated[str, AfterValidator(_public_source_url)]


class DemandCreate(BaseModel):
    """客户提交意向单（wire-ux-consolidation 任务 3.2：轻量四入参；
    wire-demand-generation-orchestration 1.1 起加合规门两入参）。

    - 需求描述（自由文本，必填——按将来对话第一条消息的形态设计）；
    - 联系邮箱（必填，最小形态校验）；
    - 公司名（选填）；
    - 目录锚点（indicator code，选填；服务端原样存储，用于预填语义）；
    - 数据源地址（≥1 个公开源 URL，必填；服务端硬校验 http(s) 与无内嵌账密）；
    - 公开数据声明（必勾——所求数据为公开可获取；False/缺失即 422）。

    旧表单字段（title/entities_hint/frequency_expect）不再收；旧调用
    携带它们会被忽略（缺必填项按 422 逐字段回显）。
    """

    description: str = Field(min_length=1)
    contact_email: str = Field(min_length=1, max_length=320)
    company: str | None = Field(default=None, max_length=200)
    indicator_code: str | None = Field(default=None, max_length=128)
    source_urls: list[SourceUrl] = Field(min_length=1)
    public_data_asserted: bool

    @field_validator("description")
    @classmethod
    def _description_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("需求描述不能为空（或全为空白字符）")
        return stripped

    @field_validator("contact_email")
    @classmethod
    def _email_well_formed(cls, value: str) -> str:
        stripped = value.strip()
        if not _EMAIL_RE.match(stripped):
            raise ValueError("联系邮箱格式不合法（应为 name@domain.tld 形态）")
        return stripped

    @field_validator("company", "indicator_code")
    @classmethod
    def _strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("public_data_asserted")
    @classmethod
    def _public_data_asserted_true(cls, value: bool) -> bool:
        """合规门：声明必须勾选——非公开数据不在代爬服务面，请线下沟通。"""
        if value is not True:
            raise ValueError(
                "必须勾选公开数据声明：仅承接公开可获取数据，非公开数据请线下沟通"
            )
        return value


class HistoryEvent(BaseModel):
    """时间线条目：{status, at, by, note}。"""

    status: str
    at: str
    by: str
    note: str | None = None


class DeliveryOut(BaseModel):
    """客户可见交付视图：不含 delivery_events 全文与失败错误串等内部细节。"""

    model_config = ConfigDict(from_attributes=True)

    scope_name: str | None = None
    #: None=未发起 / "ok"=私有 scope 已就绪 / "pending"=交付挂起重试中
    scope_status: str | None = None
    #: 授权转公开状态：None=未授权 / True=已授权 / False=已撤回
    publish_authorized: bool | None = None


class OrchestrationEventOut(BaseModel):
    """编排时间线安全条目（wire-demand-generation-orchestration 3.2）。

    只出阶段名 + 时间 + PR 链接——ticket_path、note、agent 回执全文、
    重试细节等内部留档字段绝不在此出现。
    """

    stage: str
    at: str | None = None
    pr_url: str | None = None


class DemandOut(BaseModel):
    """需求单客户视图（含客户可见时间线/联系方式/拒绝理由/交付视图）。

    绝不携带内部字段：evaluation_detail（评估全文）、quote_operator、
    quote_history、quote_amount_micro（微元整数仅内部账用）、
    pipeline_events / delivery_events 全文（内部留档）。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    customer_id: str
    title: str
    intent: str
    contact_email: str | None = None
    company: str | None = None
    entities_hint: Any = None
    frequency_expect: str | None = None
    linked_indicators: list[str] = []
    #: 数据源地址（合规门两入参，新单必返回；旧单=None）
    source_urls: list[str] | None = None
    #: 公开数据声明（新单必为 True；旧单=None）
    public_data_asserted: bool | None = None
    status: str
    status_history: list[HistoryEvent] = []
    evaluation_summary: str | None = None
    rejection_reason: str | None = None
    quote_amount: Decimal | None = None
    quote_note: str | None = None
    quote_valid_until: datetime | None = None
    #: 交付视图（任务 6.6，客户可见）：私有 scope 交付状态 + 授权转公开状态
    delivery: DeliveryOut | None = None
    #: 编排时间线（任务 3.2，客户可见安全子集）：pipeline_events 中
    #: workorder_submitted / pr_open / merged / blocked 四态映射为
    #: ``{stage, at, pr_url?}``；其余事件类型与内部字段绝不进客户面。
    orchestration: list[OrchestrationEventOut] = []
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="before")
    @classmethod
    def _derive_orchestration(cls, data: Any) -> Any:
        """响应组装点：从 pipeline_events 派生客户面编排安全子集。

        ``Demand.delivery`` 计算属性同款纪律——区别在 delivery 的源字段
        （scope_status 等）就在 ORM 上，而 orchestration 的源是内部留档
        pipeline_events 全文（客户面不可直读），故在响应模型入口过滤：
        ORM 对象 setattr 注入派生视图（不落库），dict 输入补 key，
        其余形态交缺省 []。
        """
        if isinstance(data, dict):
            if "orchestration" not in data:
                data = {
                    **data,
                    "orchestration": orchestration_customer_view(
                        data.get("pipeline_events")
                    ),
                }
            return data
        events = getattr(data, "pipeline_events", None)
        if events is not None:
            try:
                setattr(
                    data, "orchestration", orchestration_customer_view(events)
                )
            except (AttributeError, TypeError):  # pragma: no cover - 防御
                pass
        return data


class DemandInternalOut(DemandOut):
    """需求单内部完整视图（人工报价确认台用）：叠加评估全文与报价留档。"""

    evaluation_detail: dict | None = None
    quote_amount_micro: int | None = None
    quote_operator: str | None = None
    quote_history: list[dict[str, Any]] = []
    #: 流水线事件全文（任务 6.5，内部视图透出）
    pipeline_events: list[dict[str, Any]] = []
    #: 交付事件全文（任务 6.6，内部视图透出）
    delivery_events: list[dict[str, Any]] = []
    #: gate 门禁摘要：{passed, attempts, pipeline_runs, latest}
    gate: dict[str, Any] | None = None
    #: 私有 scope 交付状态（内部直读，不必进客户 delivery 视图）
    scope_status: str | None = None
    scope_name: str | None = None


class TransitionIn(BaseModel):
    """内部迁移请求：目标状态 + 说明；rejected 必带 reason（状态机裁决）。

    wire-ux-consolidation 任务 3.1 起 ``confirmed_by_operator`` 前置随
    evaluating→quoted 工作流废除（旧调用多带的字段被忽略，不影响兼容）。
    目标为废除态（evaluating 等）由状态机以 409 如实回绝，而非 422。
    """

    target_status: DemandStatus
    note: str | None = None
    by: str = "system"
    reason: str | None = None


class EvaluationIn(BaseModel):
    """AI 预评估结论概要（写入 evaluation_summary）。

    wire-ux-consolidation 任务 3.1 起：评估挂接的 evaluating 态已废除，
    本端点对现行状态一律 409（自动接单迁移 evaluating 不再放行）；
    既有 evaluation_summary/detail 字段随旧单保留可查。
    """

    summary: str = Field(min_length=1)
    by: str = "ai"
    detail: dict[str, Any] | None = None


class QuoteIn(BaseModel):
    """报价写入（旧八态工作流遗留端点）：confirmed_by_operator + operator 硬前置。

    wire-ux-consolidation 任务 3.1 起：报价不进三态状态机（走定价页自助或
    线下沟通），本端点对现行状态一律 409（须先处于 evaluating 的前置永不
    满足）；既有 quote 字段随旧单保留可查。金额约定（微元为准、兼容元参）
    维持原样。
    """

    amount_micro: int | None = Field(default=None, ge=0)
    amount: Decimal | None = Field(default=None, ge=0)
    note: str | None = None
    valid_until: datetime | None = None
    confirmed_by_operator: bool = False
    operator: str | None = Field(default=None, max_length=64)
    by: str = "operator"


class PipelineRunIn(BaseModel):
    """流水线事实记录（任务 6.5）：in_progress 期 append-only 落 pipeline_events。"""

    pipeline_run_id: str = Field(min_length=1, max_length=128)
    repo: str | None = Field(default=None, max_length=256)
    branch: str | None = Field(default=None, max_length=128)
    note: str | None = None
    by: str = "pipeline"


class GateResultIn(BaseModel):
    """conformance gate 结果（任务 6.5）：append-only 观测留档。

    wire-ux-consolidation 任务 3.1 起 gate 不再门禁状态迁移
    （building→delivery_acceptance 前置随废除态移除），仅作构建质量留痕。
    """

    passed: bool
    report_path: str | None = Field(default=None, max_length=512)
    note: str | None = None
    by: str = "pipeline"


class DeliverIn(BaseModel):
    """交付动作（任务 6.6）：in_progress→delivered + 自动建私有 scope。

    indicators 缺省用需求单的 linked_indicators；source_dbs 可选
    （scope 的 source 维 allow-list）。
    """

    by: str = "customer"
    note: str | None = None
    indicators: list[str] | None = None
    source_dbs: list[str] = Field(default_factory=list)


class PublishAuthorizationIn(BaseModel):
    """客户授权转公开 / 撤回（任务 6.6）：append-only 留档，可反复撤回再授权。"""

    granted: bool
    conditions: str | None = None
    by: str | None = Field(default=None, max_length=128)
    indicators: list[str] | None = None


class ScopeRetryIn(BaseModel):
    """scope 重试请求（wire-scope-delivery-live）：对 delivered+pending 单
    只重试建客户私有 scope（不迁移状态）。

    全字段可选（空 body 可受理）；indicators 缺省用需求单的
    linked_indicators，source_dbs 可选（scope 的 source 维 allow-list）。
    """

    by: str = "operator"
    note: str | None = None
    indicators: list[str] | None = None
    source_dbs: list[str] | None = None
