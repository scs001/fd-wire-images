"""工单生成与投递编排（wire-demand-generation-orchestration 任务 2）。

把 in_progress 需求单确定性映射成 fd-industry-data 仓 health-loop 的
「生成工单」YAML（design D3 映射表），并以**舰队拉取器**通道投递（design
D1 定案）：wire 暴露内部拉取端点（``FD_WIRE_INTERNAL_TOKEN``），由
fd-industry-data 侧拉取工作流拉工单→写 ``reports/health-tickets/``→
GITHUB_TOKEN 自推 main→回 ack；工单再经对方仓每日巡检工作流代交给萬星
生成 agent。wire 只产出与供拉取文件，绝不 merge、绝不写 schedule、
**零外部 git 凭据**（deploy key 被 org 禁、PAT 需交互铸造的调研定案，
design D6 红线）。

红线纪律：
- 脱敏（design D6）：工单全文不得含 ``contact_email``；公司名不进工单
  （brief.notes 只留 ``customer_id`` 前 8 位脱敏代号——工单进公开仓）。
- 冲突即拒：slug 在对方仓已占用（raw 探测 ``spiders/<slug>/manifest.yaml``
  返回 200）时出单拒绝转人工；网络异常按占用处理（宁可拒不可撞）。
- 诚实降级：拉取器未部署（无 ack）时出单响应照常携带工单全文供人工
  推送并明示等待状态——绝不展示任何自动化投递成功假象。
- ack 幂等：同一工单重复 ack 不重复记 ``pipeline_run`` 事件。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
import yaml
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Demand
from .source_policy import BLOCKED_SOURCE_REASON, blocked_source_domain
from .statemachine import DemandStatus
from .wanxing import HttpWanxingClient, WanxingInvokeError, get_wanxing_client

# ---------------------------------------------------------------- 常量（契约冻结）

#: 目标仓（公开镜像仓；工单由其拉取工作流落仓自推、巡检工作流代交）。
INDUSTRY_REPO_NAME = "fd-industry-data"
#: slug 占用探测：公开 raw 通道读 spiders/<slug>/manifest.yaml（200=占用）。
SLUG_PROBE_URL = (
    "https://raw.githubusercontent.com/FindDataTechnology/"
    "fd-industry-data/main/spiders/{slug}/manifest.yaml"
)
#: 工单落仓目录（对方仓拉取工作流的写入目标——wire 侧只产出路径名）。
TICKETS_DIR = "reports/health-tickets"
#: 工单文件名：``<YYYYMMDD>-wire-<demand.code>.yaml``（date=UTC 今日）。
TICKET_FILENAME_RE = re.compile(r"^[0-9]{8}-wire-[A-Za-z0-9][A-Za-z0-9.-]*\.yaml$")
#: 完整工单相对路径（拉取器按此落盘；ack 按此对账）。
TICKET_PATH_RE = re.compile(
    r"^reports/health-tickets/[0-9]{8}-wire-[A-Za-z0-9][A-Za-z0-9.-]*\.yaml$"
)
#: slug 契约：``^[a-z0-9][a-z0-9-]*$``、截 40 字符。
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SLUG_MAX_LEN = 40

#: brief.cadence 合法值（design D3：daily/hourly/weekly，非法值并入 notes）。
VALID_CADENCES = frozenset({"daily", "hourly", "weekly"})
DEFAULT_CADENCE = "daily"

#: pipeline_events 事件类型（与 pipeline.py 的常量口径一致）。
EVENT_WORKORDER_EXPORTED = "workorder_exported"
EVENT_PIPELINE_RUN = "pipeline_run"
#: 萬星直连投递事实事件（wire-wanxing-direct-dispatch）：与
#: ``workorder_exported`` 通道互斥——拉取队列只认后者，直连单零收录。
EVENT_WANXING_DISPATCHED = "wanxing_dispatched"
EVENT_WANXING_DISPATCH_FAILED = "wanxing_dispatch_failed"
#: ack 记录的 pipeline_run 事件备注（投递事实由对方仓拉取器回 ack 确认）。
WORKORDER_DELIVERED_NOTE = "workorder delivered"


class WorkorderIn(BaseModel):
    """出单请求体：slug 可覆写（操作员人工指定 unit 名），缺省从首源地址派生。"""

    slug: str | None = Field(default=None, max_length=128)


class WorkorderAckIn(BaseModel):
    """拉取器 ack 请求体：对方仓工作流落仓自推成功后回告的工单路径。"""

    ticket_path: str = Field(min_length=1, max_length=256)


class WorkorderNotFound(LookupError):
    """ack 的工单路径对不上任何导出留档——路由层转 404。"""


class WorkorderExportError(Exception):
    """出单被拒（状态门/无源地址/slug 冲突/映射失配）——路由层转 4xx。

    携带 HTTP 状态码与中文文案：内部端点与 admin 控制台端点共用同一
    service（``export_workorder_service``），拒绝口径完全一致。
    """

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class WanxingDispatchFailure(WorkorderExportError):
    """萬星直连投递失败（502 专用）——携带工单全文等 ``payload`` 随响应。

    ``WorkorderExportError`` 只有 detail 文案，装不下工单全文；而失败降级
    红线要求 502 响应**也**带全文（人工推送通道始终可用，design D2）。
    路由层捕获本类时把 ``detail`` 组装成 dict：
    ``{"message": exc.detail, **exc.payload}``（FastAPI 的 detail 合法
    可为 dict）。
    """

    def __init__(
        self, status_code: int, detail: str, *, payload: dict | None = None
    ) -> None:
        super().__init__(status_code, detail)
        self.payload = payload or {}


# ---------------------------------------------------------------- slug 与文件名


def derive_slug(source_url: str, override: str | None = None) -> str:
    """从源地址派生 spider slug：override 优先，否则取 hostname。

    净化规则：去 ``www.`` 前缀 → 非法字符（含点）压成 ``-`` → 折叠连续
    ``-``、去首尾 ``-`` → 压 ``^[a-z0-9][a-z0-9-]*$`` → 截 40 字符。
    无法派生（无主机名/净化后为空）抛 :class:`ValueError`。
    """
    candidate = (override or "").strip()
    if not candidate:
        hostname = (urlparse(source_url or "").hostname or "").lower()
        if not hostname:
            raise ValueError(
                f"无法从源地址派生 slug：{source_url!r} 没有可解析的主机名"
            )
        candidate = hostname
    if candidate.startswith("www."):
        candidate = candidate[4:]
    slug = re.sub(r"[^a-z0-9-]+", "-", candidate.lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    if len(slug) > SLUG_MAX_LEN:
        slug = slug[:SLUG_MAX_LEN].rstrip("-")
    if not slug:
        raise ValueError(
            f"无法从源地址派生 slug：{source_url!r} 净化后为空"
        )
    return slug


def ticket_filename(demand: Demand) -> str:
    """工单文件名：``<YYYYMMDD>-wire-<demand.code>.yaml``（date=UTC 今日）。"""
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"{today}-wire-{demand.code}.yaml"


def customer_code(demand: Demand) -> str:
    """客户脱敏代号：``customer_id`` 前 8 位（公司名不进公开仓，design D6）。"""
    return (demand.customer_id or "")[:8]


def demand_source_urls(demand: Demand) -> list[str]:
    """需求单源地址（合规门已校验的公开 URL）；存量单无值=空列表。"""
    return [str(u) for u in (demand.source_urls or []) if str(u).strip()]


# ---------------------------------------------------------------- 工单构造（纯函数）


def build_workorder(demand: Demand, *, slug: str | None = None) -> tuple[str, str]:
    """把需求单映射成 health-loop 生成工单 YAML（design D3 冻结契约 + 舰队 schema 合规）。

    返回 ``(yaml 文本, tickets 相对路径)``。字段集满足舰队
    ``validate_ticket`` 必填七字段（wire-workorder-ticket-conformance）：
    ``ticket_id``=文件名主干（同日稳定），``source="wire"``，``created_at``
    按每次出单时刻如实记录（同日重复出单仅 created_at 漂移；无跨次
    字节相等消费者，sha256 随每次出单在时间线留痕）。脱敏红线在映射内
    保证：``contact_email`` 与公司名绝不入单（notes/evidence 只留需求单
    编码 + 客户代号）。
    """
    source_urls = demand_source_urls(demand)
    if not source_urls:
        raise ValueError(
            f"需求单 {demand.code} 缺少 source_urls——生成工单必须来自带"
            "合法源地址的新单（存量单请人工补源地址或线下处理）"
        )
    unit_slug = slug or derive_slug(source_urls[0])
    filename = ticket_filename(demand)
    ticket_path = f"{TICKETS_DIR}/{filename}"

    # cadence：frequency_expect 映射 daily/hourly/weekly；缺省 daily；
    # 非法值不丢——原文并入 notes（design D3）。
    frequency_raw = (demand.frequency_expect or "").strip()
    cadence = (
        frequency_raw.lower()
        if frequency_raw.lower() in VALID_CADENCES
        else DEFAULT_CADENCE
    )

    # expectations：intent 原文；entities_hint 有则附实体提示（固定序列化）。
    expectations = demand.intent or ""
    if demand.entities_hint:
        expectations += (
            "\n\n实体提示："
            + json.dumps(
                demand.entities_hint, ensure_ascii=False, sort_keys=True
            )
        )

    # notes：审计线索三段——需求单编码 / 客户脱敏代号 / 未映射频率原文。
    notes_segments = [
        f"wire 需求单 {demand.code}",
        f"客户代号 {customer_code(demand)}",
    ]
    if frequency_raw and frequency_raw.lower() not in VALID_CADENCES:
        notes_segments.append(f"frequency 未映射：{frequency_raw}")

    ticket = {
        "ticket_id": filename[: -len(".yaml")],
        "source": "wire",
        "unit": f"spiders/{unit_slug}/",
        "kind": "generate",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "category": None,
        "brief": {
            "source_urls": source_urls,
            "expectations": expectations,
            "cadence": cadence,
            "notes": "；".join(notes_segments),
        },
        "golden": [f"spiders/{unit_slug}/golden/"],
        "verify": {
            "commands": [
                f"python3 scripts/health_verify.py --ticket {ticket_path}"
            ]
        },
        # evidence：舰队 generate 单惯例形状 + wire 侧脱敏事实（绝不含
        # contact_email/公司名——与 notes 同一红线）。
        "evidence": {
            "window": None,
            "failing_runs": [],
            "last_success": None,
            "demand_code": demand.code,
            "customer_code": customer_code(demand),
            "source_urls": source_urls,
            "public_data_asserted": bool(demand.public_data_asserted),
            "notes": "wire 需求单生成单（demand-orchestration）：客户需求经"
            "合规门后出单，按 brief 从零建立单元并交付 golden 样本",
        },
        "lifecycle": [],
    }
    yaml_text = yaml.safe_dump(
        ticket,
        sort_keys=False,
        allow_unicode=True,
        width=4096,
        default_flow_style=False,
    )
    return yaml_text, ticket_path


# ---------------------------------------------------------------- slug 冲突探测

#: slug 探测超时（秒）。
SLUG_PROBE_TIMEOUT = 10.0

#: 探测三态：free=确证空闲（404）/ occupied=确证占用（200 等 2xx/3xx）/
#: unverified=通道不可达（网络错/5xx）。fd-prod 到 raw.githubusercontent.com
#: 被海外出站挡死（2026-10-11 生产实测 60s 网络不可达），fail-closed 会
#: 误伤全部出单——unverified 如实放行，占用复检由舰队拉取器在本地
#: checkout 权威执行（scripts/wire_demands_pull.py）。
SlugStatus = str


def check_slug_free(slug: str, *, http_client: httpx.Client | None = None) -> SlugStatus:
    """探测 slug 在 fd-industry-data 仓的占用态（公开 raw 通道）。

    非法 slug（不合 SLUG_PATTERN）按 occupied 拒（防 URL 注入）。
    """
    if not SLUG_PATTERN.fullmatch(slug or ""):
        return "occupied"
    url = SLUG_PROBE_URL.format(slug=slug)
    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=SLUG_PROBE_TIMEOUT)
    try:
        try:
            resp = client.get(url)
        except httpx.HTTPError:
            return "unverified"
        if resp.status_code == 404:
            return "free"
        if resp.status_code >= 500:
            return "unverified"
        return "occupied"
    finally:
        if owns_client:
            client.close()


# ---------------------------------------------------------------- 出单 service（端点共用）


def _workorder_admission(
    demand: Demand, *, slug_override: str | None = None
) -> tuple[str, SlugStatus]:
    """出单闸（export 与萬星直连 dispatch 共用的唯一实现，design D2）。

    逐条照搬自 ``export_workorder_service`` 原文（零漂移重构）：状态门
    409 → 无 source_urls 422 → 禁爬源复检 409 → ``derive_slug`` 422 →
    ``check_slug_free`` occupied 409。返回 ``(slug, slug_status)``——
    export 响应要回报 ``slug_check``，一次探测两用，绝不二次探测（行为
    与抽取前逐字节一致）；失败形态一律 :class:`WorkorderExportError`、
    零留档（闸拒绝时连事件都不追加）。
    """
    current = DemandStatus(demand.status)
    if current is not DemandStatus.in_progress:
        raise WorkorderExportError(
            409,
            f"生成工单只能在 in_progress 状态导出（当前 {current.value}）"
            "——工单属于处理期（出单不迁移状态）",
        )
    source_urls = demand_source_urls(demand)
    if not source_urls:
        raise WorkorderExportError(
            422,
            f"需求单 {demand.code} 没有 source_urls（存量单无源地址）——"
            "生成工单必须来自带合法源地址的新单；存量单请人工补源地址"
            "或线下处理",
        )
    # 禁爬源复检（wire-crawl-source-blocklist）：存量单可能绕过提交口
    # （历史遗留/直接入库）——出单闸是最后一道，命中即拒、零留档。
    for url in source_urls:
        blocked = blocked_source_domain(url)
        if blocked is not None:
            raise WorkorderExportError(
                409,
                f"需求单 {demand.code} 的源地址命中禁爬平台 {blocked}"
                f"——{BLOCKED_SOURCE_REASON}，不能出单（请与客户沟通撤单或换源）",
            )
    try:
        slug = derive_slug(source_urls[0], override=slug_override)
    except ValueError as exc:
        raise WorkorderExportError(422, str(exc)) from exc
    slug_status = check_slug_free(slug)
    if slug_status == "occupied":
        raise WorkorderExportError(
            409,
            f"slug {slug} 在 {INDUSTRY_REPO_NAME} 仓内已占用——"
            "非 greenfield 不走生成流，请转人工处理",
        )
    return slug, slug_status


def export_workorder_service(
    db: Session,
    demand: Demand,
    *,
    slug_override: str | None = None,
    trigger: str | None = None,
) -> dict:
    """出单 service：确定性映射 + append-only 留档入队（端点唯一实现）。

    ``/api/v1/demands/{id}/workorder``（内部令牌）与
    ``/internal/admin/demands/{id}/workorder``（admin 双闸）共用本函数——
    语义、响应形状、拒绝口径（:class:`WorkorderExportError`）完全一致：

    - 出单闸（状态门/无源地址/禁爬复检/slug 三态探测）在
      :func:`_workorder_admission` ——与萬星直连 dispatch 同一拒绝口径；
    - 出单 → ``workorder_exported`` 全文+哈希 append-only 留档即入队，
      同一需求单可重出新工单、历史不改写、状态不迁移；
    - 响应恒带工单全文供人工推送降级——出单时刻未投递（``delivered``
      =false），绝不展示自动化投递成功假象。

    ``trigger``（wire-demand-auto-export）：自动补扫出单传 ``"auto"``，
    导出事件带 ``trigger`` 键供运营面标注来源；缺省 None 时事件形状与
    人工出单路径逐字节一致（零漂移）。
    """
    slug, slug_status = _workorder_admission(demand, slug_override=slug_override)
    try:
        ticket_yaml, ticket_path = build_workorder(demand, slug=slug)
    except ValueError as exc:
        raise WorkorderExportError(422, str(exc)) from exc
    sha256 = hashlib.sha256(ticket_yaml.encode("utf-8")).hexdigest()
    event: dict = {
        "type": EVENT_WORKORDER_EXPORTED,
        "ticket_path": ticket_path,
        "ticket_yaml": ticket_yaml,
        "sha256": sha256,
    }
    if trigger is not None:
        event["trigger"] = trigger
    demand.append_pipeline_event(event)
    db.commit()  # 工单不丢：exported 事件落库即入拉取队列
    db.refresh(demand)
    return {
        "ticket_yaml": ticket_yaml,  # 全文恒随响应返回：人工推送降级始终可用
        "ticket_path": ticket_path,
        "delivered": False,  # 投递由舰队拉取器 ack 确认——出单时刻未投递
        "queued_for_pull": True,  # 已入队：等待拉取器（~30min）或人工推送
        "slug_check": slug_status,  # free=已确证空闲；unverified=探测通道
        # 不可达如实放行，占用复检在拉取器本地 checkout 权威执行
    }


# ---------------------------------------------------------------- 萬星直连调度 service（design D2）


def dispatch_wanxing_service(
    db: Session,
    demand: Demand,
    *,
    slug_override: str | None = None,
) -> dict:
    """萬星直连调度 service：同映射同红线 → A2A message/send → 留痕可审。

    ``/api/v1/demands/{id}/wanxing-dispatch``（内部令牌）与
    ``/internal/admin/demands/{id}/wanxing-dispatch``（admin 双闸）共用
    本函数（双闸唯一实现）。与舰队拉取通道（export→拉取器→ack）互斥：
    操作员显式选通道，事件类型天然区分，直连单绝不进拉取队列。

    - 出单闸复用 :func:`_workorder_admission`——与「转生成工单」同一
      拒绝口径（409/422、零留档）；
    - 工单由同一 ``build_workorder`` 纯映射产出（脱敏红线同源保证：
      直连发送的就是工单 YAML 全文，里面本无邮箱/公司名）；
    - 幂等键 ``wire-{code}-{sha256(yaml)[:16]}``（同工单内容重发同键，
      wanxing 侧去重口径）、rpc_id ``wire-{code}-{sha[:8]}``；
    - 未配置（键缺席）→ 503 如实回绝、零留档（纯配置错误，无投递事实
      可记）；调用失败 → ``wanxing_dispatch_failed`` 事件（error/reason/
      全文随档）+ :class:`WanxingDispatchFailure`（502 响应也带全文）；
    - 成功 → ``wanxing_dispatched`` 事件（路径/全文/哈希/幂等键/
      message_id/http_status/reply_excerpt）append-only 留档，状态不迁移；
      **绝不追加** ``workorder_exported``（拉取队列只认它）。
    """
    slug, _slug_status = _workorder_admission(demand, slug_override=slug_override)
    ticket_yaml, ticket_path = build_workorder(demand, slug=slug)
    sha256 = hashlib.sha256(ticket_yaml.encode("utf-8")).hexdigest()
    idempotency_key = f"wire-{demand.code}-{sha256[:16]}"
    rpc_id = f"wire-{demand.code}-{sha256[:8]}"

    client = get_wanxing_client()
    if client is None:
        # 未配置不是投递事实：零留档，明示走舰队通道或补配置。
        raise WorkorderExportError(
            503,
            "萬星直连未配置（WANXING_CALLER_KEY 缺席）——"
            "请走舰队拉取通道或配置调用键",
        )
    try:
        receipt = client.send_message(
            ticket_yaml, idempotency_key=idempotency_key, rpc_id=rpc_id
        )
    except WanxingInvokeError as exc:
        # 失败留档（与 scope 交付失败同哲学：时间线是审计事实流）——
        # 全文随档，人工推送降级通道始终可用。
        demand.append_pipeline_event(
            {
                "type": EVENT_WANXING_DISPATCH_FAILED,
                "ticket_path": ticket_path,
                "ticket_yaml": ticket_yaml,
                "sha256": sha256,
                "slug": slug,
                "error": str(exc)[:500],
                "reason": exc.reason,
            }
        )
        db.commit()
        db.refresh(demand)
        raise WanxingDispatchFailure(
            502,
            f"萬星直连投递失败（{exc.reason or exc.status_code}）：{exc}",
            payload={
                "ticket_yaml": ticket_yaml,
                "ticket_path": ticket_path,
                "sha256": sha256,
                "slug": slug,
            },
        ) from exc
    finally:
        if isinstance(client, HttpWanxingClient):
            client.close()  # per-request 客户端用完即弃（对齐 HttpScopeClient）

    reply_text = str(receipt.get("reply_text") or "")
    demand.append_pipeline_event(
        {
            "type": EVENT_WANXING_DISPATCHED,
            "ticket_path": ticket_path,
            "ticket_yaml": ticket_yaml,
            "sha256": sha256,
            "slug": slug,
            "idempotency_key": idempotency_key,
            "message_id": receipt.get("message_id"),
            "http_status": receipt.get("http_status"),
            "reply_excerpt": reply_text[:500],
        }
    )
    db.commit()  # 投递事实不丢：dispatched 事件落库即审计留痕
    db.refresh(demand)
    return {
        "ticket_path": ticket_path,
        "ticket_yaml": ticket_yaml,  # 全文恒随响应（与 export 同降级哲学）
        "slug": slug,
        "sha256": sha256,
        "delivered": True,  # A2A 同步回执 2xx 即投递事实
        "queued_for_pull": False,  # 直连单不进舰队拉取队列（事件类型互斥）
        "a2a": {
            "http_status": receipt.get("http_status"),
            "message_id": receipt.get("message_id"),
            "idempotency_key": idempotency_key,
            "reply_excerpt": reply_text[:500],
        },
    }


# ---------------------------------------------------------------- 拉取队列（pending / ack）


def _latest_export_index(
    events: list[dict], ticket_path: str | None = None
) -> int | None:
    """事件流里最新一条 ``workorder_exported`` 的下标（可按路径过滤）。"""
    index: int | None = None
    for idx, event in enumerate(events):
        if event.get("type") != EVENT_WORKORDER_EXPORTED:
            continue
        if ticket_path is not None and event.get("ticket_path") != ticket_path:
            continue
        index = idx
    return index


def _acked_after(events: list[dict], export_idx: int) -> bool:
    """导出之后是否已有对应 ack（pipeline_run 且 id==工单文件名）。"""
    filename = (events[export_idx].get("ticket_path") or "").rsplit("/", 1)[-1]
    return any(
        event.get("type") == EVENT_PIPELINE_RUN
        and event.get("pipeline_run_id") == filename
        for event in events[export_idx + 1 :]
    )


def latest_unacked_export(events: list[dict]) -> dict | None:
    """事件流里最新一条未被 ack 的 ``workorder_exported``（None=无待投）。

    ack 判定按事件序（append-only 下标）：该导出**之后**存在
    ``pipeline_run`` 且 ``pipeline_run_id`` == 工单文件名。同日重出新单
    （同路径新导出）因此不会误吃旧 ack；更新导出天然取代旧导出。
    """
    export_idx = _latest_export_index(events)
    if export_idx is None or _acked_after(events, export_idx):
        return None
    return events[export_idx]


def pending_workorders(db: Session) -> list[dict]:
    """待投递工单清单：仅 in_progress 单、每单至多一条最新未 ack 导出。

    拉取器（fd-industry-data 侧工作流）按此清单落仓自推；导出留档即
    入队——无需额外队列表。
    """
    stmt = (
        select(Demand)
        .where(Demand.status == DemandStatus.in_progress.value)
        .order_by(Demand.id)
    )
    pending: list[dict] = []
    for demand in db.scalars(stmt):
        export = latest_unacked_export(list(demand.pipeline_events or []))
        if export is None:
            continue
        pending.append(
            {
                "demand_id": demand.id,
                "code": demand.code,
                "ticket_path": export.get("ticket_path"),
                "sha256": export.get("sha256"),
                "ticket_yaml": export.get("ticket_yaml"),
                "exported_at": export.get("at"),
            }
        )
    return pending


def ack_workorder(db: Session, ticket_path: str) -> dict:
    """拉取器回 ack：工单已由对方仓落仓自推——wire 记 ``pipeline_run``。

    - 查无该路径的导出留档 → :class:`WorkorderNotFound`（未知工单）；
    - 幂等（spec 场景「ack 幂等」）：该路径**最新导出**已 ack 时重复
      ack 不重复追加事件，返回既有已 ack 状态；同日重出新单（ack 之后
      的新导出）再 ack 会如实记第二次投递事实；
    - 投递是既成事实的观测留档：即使需求单已迁出 in_progress 也照记。
    """
    normalized = (ticket_path or "").strip()
    if not TICKET_PATH_RE.fullmatch(normalized):
        raise WorkorderNotFound(
            f"工单路径 {ticket_path!r} 形态不合契约"
            f"（{TICKETS_DIR}/<YYYYMMDD>-wire-<code>.yaml）"
        )
    demand = _demand_for_ticket(db, normalized)
    if demand is None:
        raise WorkorderNotFound(
            f"工单 {ticket_path!r} 没有任何导出留档（未知工单）"
        )
    events = list(demand.pipeline_events or [])
    export_idx = _latest_export_index(events, ticket_path=normalized)
    assert export_idx is not None  # _demand_for_ticket 命中即有该路径导出
    filename = normalized.rsplit("/", 1)[-1]
    if _acked_after(events, export_idx):  # 幂等：最新导出已 ack
        return {
            "demand_id": demand.id,
            "ticket_path": normalized,
            "pipeline_run_id": filename,
            "acked": True,
            "already_acked": True,
        }
    demand.append_pipeline_event(
        {
            "type": EVENT_PIPELINE_RUN,
            "pipeline_run_id": filename,
            "repo": INDUSTRY_REPO_NAME,
            "branch": "main",
            "note": WORKORDER_DELIVERED_NOTE,
            "pr_url": None,
        }
    )
    db.commit()
    db.refresh(demand)
    return {
        "demand_id": demand.id,
        "ticket_path": normalized,
        "pipeline_run_id": filename,
        "acked": True,
        "already_acked": False,
    }


def _demand_for_ticket(db: Session, ticket_path: str) -> Demand | None:
    """找到导出过该工单路径的需求单（路径含业务编码，天然一对一）。"""
    for demand in db.scalars(select(Demand).order_by(Demand.id)):
        for event in demand.pipeline_events or []:
            if (
                event.get("type") == EVENT_WORKORDER_EXPORTED
                and event.get("ticket_path") == ticket_path
            ):
                return demand
    return None


# ---------------------------------------------------------------- 推送式状态回流（tracked / status）


def tracked_workorders(db: Session) -> list[str]:
    """在途工单路径清单：in_progress 单的最新导出（含已 ack——状态演化
    发生在投递之后），拉取器据此从其本地 checkout 读文件回推状态。

    wire 出站到 raw.githubusercontent.com 在 fd-prod 被海外出站挡死
    （2026-10-11 实测），状态回流因此改为**舰队推送式**：拉取器有网，
    它读本地文件 POST 给 wire，wire 零出站。B 节的轮询循环保留为
    有出站能力环境下的兜底（生产默认关）。
    """
    stmt = (
        select(Demand)
        .where(Demand.status == DemandStatus.in_progress.value)
        .order_by(Demand.id)
    )
    tracked: list[str] = []
    for demand in db.scalars(stmt):
        events = list(demand.pipeline_events or [])
        if any(e.get("type") == "workorder_lost" for e in events):
            continue  # 契约失配停扫：等人工处置
        export_idx = _latest_export_index(events)
        if export_idx is None:
            continue
        path = events[export_idx].get("ticket_path")
        if path:
            tracked.append(path)
    return tracked


def ingest_ticket_status(db: Session, ticket_path: str, content: str) -> dict:
    """拉取器回推的工单文件全文 → 解析 → 映射事件幂等落库。

    - 路径形态非法/无导出留档 → :class:`WorkorderNotFound`（404）；
    - YAML 解析失败 → :class:`ValueError`（400 如实回绝，拉取器留日志）；
    - 幂等：映射事件按 (type, ticket_path) 与既有 pipeline_events 去重，
      重复推送零追加（拉取器每 30min 全量回推，这是常态路径）。
    """
    from . import orchestration_sync  # 局部导入避免加载期环

    normalized = (ticket_path or "").strip()
    if not TICKET_PATH_RE.fullmatch(normalized):
        raise WorkorderNotFound(
            f"工单路径 {ticket_path!r} 形态不合契约"
            f"（{TICKETS_DIR}/<YYYYMMDD>-wire-<code>.yaml）"
        )
    demand = _demand_for_ticket(db, normalized)
    if demand is None:
        raise WorkorderNotFound(
            f"工单 {ticket_path!r} 没有任何导出留档（未知工单）"
        )
    try:
        ticket = yaml.safe_load(content or "")
    except yaml.YAMLError as exc:
        raise ValueError(f"工单 YAML 解析失败：{exc}") from exc
    if not isinstance(ticket, dict):
        raise ValueError("工单 YAML 顶层不是映射（契约失配，如实回绝）")
    mapped = orchestration_sync.map_ticket_to_events(ticket)
    events = list(demand.pipeline_events or [])
    seen = {
        (e.get("type"), e.get("ticket_path")) for e in events
    }
    appended = 0
    for ev in mapped:
        ev = {**ev, "ticket_path": normalized}
        if (ev.get("type"), normalized) in seen:
            continue
        demand.append_pipeline_event(ev)
        seen.add((ev.get("type"), normalized))
        appended += 1
    if appended:
        db.commit()
        db.refresh(demand)
    return {
        "demand_id": demand.id,
        "ticket_path": normalized,
        "events_appended": appended,
    }
