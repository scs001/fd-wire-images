"""工单状态回流同步（wire-demand-generation-orchestration 任务 3）。

需求单投递生成工单到 fd-industry-data 公开仓后，工单 YAML 文件自身就是
生命周期的事实源（``lifecycle`` append-only 序列 + ``terminal`` 终态字段，
契约见 ``reports/health-tickets/*.yaml`` 真实样本）。本模块周期把这份
**真实状态**回流成 wire 侧 ``pipeline_events`` 追加事件，并给客户详情
提供安全子集时间线——绝不在无真实来源时编造任何进度（spec：「状态回流
SHALL 只写真实进度」）。

同步对象（候选单，条件缺一不可）：

- in_progress 需求单（处理期）；
- ``pipeline_events`` 里有 ``workorder_exported``（出单留档，含 ticket_path）；
- 其后有 ``pipeline_run`` 事件（舰队拉取器 ack 的事实——工单文件确已
  落对方仓 main；未 ack 的出单不扫，避免把「尚未投递」误判 404 失踪）；
- 且无 ``workorder_lost``（契约失配留痕后该单停扫，等人工处置）。

事件词汇（与 spec 冻结一致；全部 append-only，按 ``(type, ticket_path)``
幂等去重——同键已存在即跳过）：

===============  =========================================================
工单事实         wire 侧事件
===============  =========================================================
首次 submitted   ``workorder_submitted``（已进入生成流水线）
  ok:true
首次非空 pr_url  ``pr_open``（爬虫已生成，PR 待人审）
或 state=done
terminal=closed  ``merged``（人审合并收口）
submitted ≥3 次  ``blocked``（生成受阻，运营跟进）
全 ok:false
拉取 404         ``workorder_lost``（契约失配留痕，该单停扫转人工）
===============  =========================================================

失败语义（design D5）：网络/5xx 拉取失败本轮跳过零写入、下轮重试；YAML
解析失败同样零写入（不猜状态）；404 只写一次 ``workorder_lost``。终态
交付仍由操作员经既有交付端点完成——本模块绝不改需求单状态、绝不自动
置 delivered。

lifespan 驱动（metering/driver.py 同构）：``start_workorder_sync()`` /
``stop_workorder_sync()``，tick 缺省 600s（``WORKORDER_SYNC_INTERVAL_SECONDS``
覆盖）、``WORKORDER_SYNC_ENABLED=false`` 关闭。
"""

from __future__ import annotations

import asyncio
import logging
import os
from urllib.parse import quote

import httpx
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import SessionLocal, init_db
from .models import Demand
from .statemachine import DemandStatus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 契约常量（冻结）

#: 工单 raw 拉取基址（公开镜像仓，免凭据；ticket_path 为仓内完整路径
#: ``reports/health-tickets/<file>.yaml``）。
RAW_BASE_URL = (
    "https://raw.githubusercontent.com/FindDataTechnology/fd-industry-data/main"
)
#: 工单目录（与 orchestration.TICKETS_DIR 同值；本地硬编码避免与并行
#: 改动中的 orchestration 模块产生导入耦合）。
TICKETS_DIR = "reports/health-tickets"

# 事件类型（wire 侧 pipeline_events；workorder_exported/pipeline_run 由
# 出单/ack 流程写入，本模块只读）。
EVENT_WORKORDER_EXPORTED = "workorder_exported"
EVENT_PIPELINE_RUN = "pipeline_run"
EVENT_WORKORDER_SUBMITTED = "workorder_submitted"
EVENT_PR_OPEN = "pr_open"
EVENT_MERGED = "merged"
EVENT_BLOCKED = "blocked"
EVENT_WORKORDER_LOST = "workorder_lost"

#: 客户面安全子集：只有这四个阶段进 orchestration 时间线；workorder_exported
#: / pipeline_run / gate_result / workorder_lost 及一切内部留档原文绝不外露。
CUSTOMER_STAGES = frozenset(
    {EVENT_WORKORDER_SUBMITTED, EVENT_PR_OPEN, EVENT_MERGED, EVENT_BLOCKED}
)

#: 拉取超时（秒）与重试次数（首次 + 2 重试 = 单轮至多 3 次请求）。
FETCH_TIMEOUT_SECONDS = 15.0
FETCH_RETRIES = 2

#: 同步循环缺省 tick 间隔（秒）。
DEFAULT_INTERVAL_SECONDS = 600.0


# ---------------------------------------------------------------- 拉取异常


class _TicketLost(Exception):
    """工单文件从目标仓消失（raw 404）——契约失配，留痕转人工。"""


class _TicketUnavailable(Exception):
    """拉取暂不可用（网络/5xx/限流）——本轮跳过零写入，下轮重试。"""


# ---------------------------------------------------------------- 纯函数（可测核心）


def map_ticket_to_events(ticket: dict) -> list[dict]:
    """工单 YAML dict → 待追加事件列表（纯函数，IO 全在外层）。

    返回条目只含 ``type`` 与可选 ``pr_url`` / ``note``——``at`` 由
    ``Demand.append_pipeline_event`` 盖章、``ticket_path`` 由调用方补
    （工单文件不自含自身路径）。任何无法解读的形态一律返回更少的事件，
    绝不猜状态。
    """
    lifecycle = ticket.get("lifecycle")
    entries = [e for e in lifecycle if isinstance(e, dict)] if isinstance(lifecycle, list) else []

    events: list[dict] = []

    # workorder_submitted：首次 submitted ok:true（一次成功即视为已进流水线）。
    submitted = [e for e in entries if e.get("event") == "submitted"]
    has_success = any(e.get("ok") is True for e in submitted)
    if has_success:
        events.append({"type": EVENT_WORKORDER_SUBMITTED})

    # pr_open：首次出现非空 pr_url，或 state=done（生成完成）。pr_url 取
    # lifecycle 中第一个非空值（触发源与链接可以不在同一条目上）。
    pr_url = next(
        (
            str(e.get("pr_url")).strip()
            for e in entries
            if str(e.get("pr_url") or "").strip()
        ),
        "",
    )
    state_done = any(e.get("state") == "done" for e in entries)
    if pr_url or state_done:
        pr_open: dict = {"type": EVENT_PR_OPEN}
        if pr_url:
            pr_open["pr_url"] = pr_url
        events.append(pr_open)

    # merged：terminal=closed（人审合并收口）；PR 链接一并留档（若有）。
    if ticket.get("terminal") == "closed":
        merged: dict = {"type": EVENT_MERGED}
        if pr_url:
            merged["pr_url"] = pr_url
        events.append(merged)

    # blocked：submitted 尝试 ≥3 次全 ok:false 且无任何成功。
    if not has_success and len(submitted) >= 3:
        events.append(
            {
                "type": EVENT_BLOCKED,
                "note": (
                    f"工单提交连续 {len(submitted)} 次未成功"
                    "（lifecycle submitted 全部 ok:false）——运营跟进"
                ),
            }
        )

    return events


def orchestration_customer_view(events: list | None) -> list[dict]:
    """pipeline_events 原文 → 客户可见编排时间线（安全子集，纯函数）。

    只出 ``{stage, at, pr_url?}``：阶段名限定四态安全词汇，其余事件类型
    （workorder_exported / pipeline_run / gate_result / workorder_lost）
    及一切内部字段（ticket_path、note、agent 回执、重试细节）绝不进客户面。
    """
    view: list[dict] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        stage = event.get("type")
        if stage not in CUSTOMER_STAGES:
            continue
        entry = {"stage": stage, "at": event.get("at")}
        pr_url = event.get("pr_url")
        if pr_url:
            entry["pr_url"] = pr_url
        view.append(entry)
    return view


# ---------------------------------------------------------------- 同步主体


def _raw_url(ticket_path: str) -> str:
    """工单完整路径 → 公开 raw URL（容忍只带文件名的旧事件形态）。"""
    path = (ticket_path or "").strip().replace("\\", "/").lstrip("/")
    if not path.startswith(f"{TICKETS_DIR}/"):
        path = f"{TICKETS_DIR}/{path}"
    return f"{RAW_BASE_URL}/{quote(path, safe='/')}"


def _fetch_raw_ticket(
    ticket_path: str, *, http_client: httpx.Client | None = None
) -> str:
    """拉工单 YAML 原文（15s 超时、2 次重试）。

    - 404 → :class:`_TicketLost`（立即，不重试——文件消失不是瞬时故障）；
    - 网络/5xx/其它 4xx → 重试耗尽抛 :class:`_TicketUnavailable`（本轮零写入）。
    """
    url = _raw_url(ticket_path)
    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=FETCH_TIMEOUT_SECONDS)
    last_reason = "unknown"
    try:
        for _ in range(FETCH_RETRIES + 1):
            try:
                resp = client.get(url)
            except httpx.HTTPError as exc:
                last_reason = f"{type(exc).__name__}: {exc}"
                continue
            if resp.status_code == 404:
                raise _TicketLost(ticket_path)
            if resp.status_code >= 400:
                last_reason = f"HTTP {resp.status_code}"
                continue
            return resp.text
        raise _TicketUnavailable(f"{ticket_path}: {last_reason}")
    finally:
        if owns_client:
            client.close()


def _latest_exported_ticket_path(demand: Demand) -> str | None:
    """最新一条 workorder_exported 的 ticket_path（同单可重出新工单）。"""
    path = None
    for event in demand.pipeline_events or []:
        if not isinstance(event, dict):
            continue
        if event.get("type") == EVENT_WORKORDER_EXPORTED:
            candidate = str(event.get("ticket_path") or "").strip()
            if candidate:
                path = candidate
    return path


def _find_candidates(db: Session) -> list[Demand]:
    """已投递工单的在途需求单（见模块 docstring 四条件）。"""
    demands = list(
        db.scalars(
            select(Demand)
            .where(Demand.status == DemandStatus.in_progress.value)
            .order_by(Demand.id)
        )
    )
    candidates: list[Demand] = []
    for demand in demands:
        events = [
            e for e in (demand.pipeline_events or []) if isinstance(e, dict)
        ]
        if any(e.get("type") == EVENT_WORKORDER_LOST for e in events):
            continue  # 失配留痕后该单停扫
        exported = [
            i for i, e in enumerate(events) if e.get("type") == EVENT_WORKORDER_EXPORTED
        ]
        if not exported:
            continue
        last_export = max(exported)
        if not any(
            e.get("type") == EVENT_PIPELINE_RUN for e in events[last_export + 1:]
        ):
            continue  # 最新工单尚未 ack（未真实落仓）——不扫
        candidates.append(demand)
    return candidates


def _append_event_once(demand: Demand, event: dict) -> bool:
    """幂等追加：同 ``(type, ticket_path)`` 已存在即跳过，返回是否新增。

    绝不改写既有条目（append-only 审计纪律）；``at`` 由 append_pipeline_event
    盖章。
    """
    existing = demand.pipeline_events or []
    if any(
        isinstance(e, dict)
        and e.get("type") == event.get("type")
        and e.get("ticket_path") == event.get("ticket_path")
        for e in existing
    ):
        return False
    demand.append_pipeline_event(event)
    return True


def sync_demand_workorders(
    db: Session, *, http_client: httpx.Client | None = None
) -> dict[str, int]:
    """一轮同步：扫候选单 → 拉工单 raw → 按映射幂等追加事件。

    返回 ``{"checked": n, "updated": n, "skipped": n}``。拉取失败（网络/
    5xx）与解析失败本轮零写入；404 只写一次 ``workorder_lost``（之后该单
    不再进候选）。不迁移任何需求单状态。
    """
    summary = {"checked": 0, "updated": 0, "skipped": 0}
    for demand in _find_candidates(db):
        summary["checked"] += 1
        ticket_path = _latest_exported_ticket_path(demand)
        if not ticket_path:
            summary["skipped"] += 1
            continue
        try:
            raw = _fetch_raw_ticket(ticket_path, http_client=http_client)
        except _TicketLost:
            lost = _append_event_once(
                demand,
                {
                    "type": EVENT_WORKORDER_LOST,
                    "ticket_path": ticket_path,
                    "note": (
                        "工单文件从目标仓消失（raw 404）——外部契约失配，"
                        "停扫转人工处置"
                    ),
                },
            )
            if lost:
                db.commit()
                summary["updated"] += 1
                logger.warning(
                    "demand %s 工单 %s 拉取 404：已留痕 workorder_lost，停扫转人工",
                    demand.code,
                    ticket_path,
                )
            else:
                summary["skipped"] += 1
            continue
        except _TicketUnavailable as exc:
            logger.warning(
                "demand %s 工单 %s 本轮拉取失败（%s）：零写入，下轮重试",
                demand.code,
                ticket_path,
                exc,
            )
            summary["skipped"] += 1
            continue
        try:
            ticket = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            logger.warning(
                "demand %s 工单 %s YAML 解析失败（%s）：本轮跳过零写入",
                demand.code,
                ticket_path,
                exc,
            )
            summary["skipped"] += 1
            continue
        if not isinstance(ticket, dict):
            logger.warning(
                "demand %s 工单 %s 载荷不是 YAML 映射：%r——本轮跳过零写入",
                demand.code,
                ticket_path,
                type(ticket).__name__,
            )
            summary["skipped"] += 1
            continue

        appended_any = False
        for event in map_ticket_to_events(ticket):
            appended_any = (
                _append_event_once(demand, {**event, "ticket_path": ticket_path})
                or appended_any
            )
        if appended_any:
            db.commit()
            summary["updated"] += 1
        else:
            summary["skipped"] += 1
    return summary


# ---------------------------------------------------------------- lifespan 驱动
# （metering/driver.py 同构：async 循环 tick、阻塞 IO 下放线程、异常只记
# 日志下轮再试。）


_task: asyncio.Task[None] | None = None


def _interval_seconds() -> float:
    raw = os.environ.get("WORKORDER_SYNC_INTERVAL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_INTERVAL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_INTERVAL_SECONDS
    return value if value > 0 else DEFAULT_INTERVAL_SECONDS


def _sync_enabled() -> bool:
    return os.environ.get("WORKORDER_SYNC_ENABLED", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _tick_once() -> dict[str, int] | None:
    """一轮同步（阻塞 IO）：自开 demands 会话；返回摘要。"""
    init_db()
    session = SessionLocal()
    try:
        return sync_demand_workorders(session)
    finally:
        session.close()


async def _loop() -> None:
    interval = _interval_seconds()
    while True:
        await asyncio.sleep(interval)
        try:
            # sync_demand_workorders 是阻塞 IO（httpx + SQLAlchemy）——下放线程
            await asyncio.to_thread(_tick_once)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 失败只记日志，等下一轮
            logger.exception("workorder sync tick crashed; will retry next interval")


def start_workorder_sync() -> None:
    """启动工单状态回流循环（幂等；需在事件循环内；WORKORDER_SYNC_ENABLED=false 关闭）。"""
    global _task
    if not _sync_enabled():
        logger.info("workorder sync disabled by env")
        return
    if _task is not None and not _task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("no running event loop; workorder sync not started")
        return
    _task = loop.create_task(_loop())
    logger.info("workorder sync started (interval=%ss)", _interval_seconds())


def stop_workorder_sync() -> None:
    global _task
    if _task is not None and not _task.done():
        _task.cancel()
    _task = None
