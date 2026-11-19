"""WAL 转发器：引擎 cell_event_wal → 平台账本（D3 两段式采集下半段，任务 3.3）。

采集链路（design.md D3）：引擎侧 WAL 与变更同事务写（崩溃零丢失），
本模块异步拉取未转发页并 append 平台账本——**最坏滞后、绝不丢事件**：

- 游标在引擎侧：``customer_dataset_provenance_wal_pending`` 只返回未 ack
  事件，平台恒以 ``after_seq=0`` 拉取；推进游标的唯一动作是
  ``customer_dataset_provenance_wal_ack``（幂等），平台不另存游标。
- 幂等去重：append_event 复用 WAL 的 ``event_id``——ack 失败/中途崩溃
  后下一轮重拉，已入账条目按 ``event_id`` 直接返回既有行，零重复入账。
- 档位门只作用于引擎修正类事件：``off`` 档跳过 append 但照常计入待 ack
  （不留积压）；数据集行不存在（已删除等）按默认档 ``chain_only``
  入账，不静默丢事件。
- ack 的正确性前提是先落账：**每条入账即时 commit**（append_event 只
  flush 不 commit 的"事务边界归调用方"语义在此兑现——转发器是调用方，
  且 ack 语义是"账本已接管"，未落账就 ack 会永久丢事件）；中途 append
  异常则 rollback 后 ack 停在最后一条成功落账的事件。
- 引擎不可达/旧引擎无 WAL 工具：记 ``ForwardStats.errors`` 跳过该客户，
  绝不抛出（滞后作为可观测指标暴露）。

后台轮询：``start_forwarder_loop`` / ``stop_forwarder_loop`` 模块级
start/stop（接线由 app 生命周期统一负责，本模块不自挂）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai.settings import get_settings as get_ai_settings
from ..ai.settings import parse_endpoint_map
from ..datasets.db import get_session
from ..datasets.ingest import get_engine_client_factory
from ..datasets.models import Dataset
from .ledger import append_event
from .models import LedgerEvent

logger = logging.getLogger("fd_wire_api.provenance.forwarder")

#: 引擎 WAL 工具全名（customer_dataset 组注册名，与 ingest.py 的
#: customer_dataset_* 工具同款前缀规则；引擎侧实现见契约，勿在 api 侧改名）
TOOL_WAL_PENDING = "customer_dataset_provenance_wal_pending"
TOOL_WAL_ACK = "customer_dataset_provenance_wal_ack"

#: 单页拉取上限（引擎侧钳制 [1,1000]，200 与契约示例一致）
_PAGE_LIMIT = 200
#: 单轮单客户页数护栏：转发期间新事件持续写入时不让 drain 循环空转
_MAX_PAGES_PER_ROUND = 50

#: 轮询间隔 env（秒；非法/缺省回落 30）
_INTERVAL_ENV = "FD_WIRE_PROVENANCE_FORWARD_INTERVAL_SECONDS"
_DEFAULT_INTERVAL_SECONDS = 30


@dataclass
class ForwardStats:
    """单轮转发统计（滞后可观测的原料）。

    - ``customers``：本轮枚举到并尝试转发的客户数；
    - ``appended``：新入账事件数（event_id 幂等命中的重放不计）；
    - ``skipped_off``：off 档跳过 append 的事件数（照常计入待 ack）；
    - ``acked_upto``：customer_id → 本轮 ack 到的 WAL seq（只记最后成功值）；
    - ``errors``：失败摘要（引擎不可达/旧引擎无 WAL 工具/入账异常），
      一条一客户一事，滞后排障入口。
    """

    customers: int = 0
    appended: int = 0
    skipped_off: int = 0
    acked_upto: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def _interval_seconds() -> int:
    """轮询间隔：env 可覆盖，非法值如实回落默认（不因配置笔误停摆）。"""
    raw = os.getenv(_INTERVAL_ENV, "")
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_INTERVAL_SECONDS
    return value if value >= 1 else _DEFAULT_INTERVAL_SECONDS


def _forward_customers(db: Session) -> list[str]:
    """枚举有引擎端点的客户（AI_CELL_ENDPOINT_MAP 与 ingest 通道同源）。

    map 已配：map 键即显式开通口径（未命中客户 resolve_cell_endpoint 返
    None → EngineUnavailable → 落 stats.errors，如实可见）。map 未配
    （约定部署）：任何客户都可派生端点，按 datasets 表登记客户枚举；
    未注册 cell 的派生端点握手期 404/405，同样落 errors 不抛出。
    """
    mapping = parse_endpoint_map(get_ai_settings().ai_cell_endpoint_map)
    if mapping:
        return sorted(mapping)
    return sorted(
        db.execute(select(Dataset.customer_id).distinct()).scalars().all()
    )


def _tier_of(
    db: Session, customer_id: str, dataset_key: str, cache: dict[str, str]
) -> str:
    """查档位（页内缓存）；行缺失按默认档 chain_only——不静默丢事件。"""
    if dataset_key not in cache:
        tier = db.execute(
            select(Dataset.provenance_tier).where(
                Dataset.customer_id == customer_id,
                Dataset.key == dataset_key,
            )
        ).scalar_one_or_none()
        cache[dataset_key] = tier if tier is not None else "chain_only"
    return cache[dataset_key]


def _append_wal_event(
    db: Session,
    customer_id: str,
    event: dict,
    tiers: dict[str, str],
    stats: ForwardStats,
) -> None:
    """单条 WAL 事件入账（或 off 档跳过）；成功路径即时 commit。

    ``event_id`` 复用 WAL 的 event_id（幂等重试的关键）；重复拉取时
    append_event 返回既有行，``stats.appended`` 只计新增。
    """
    payload = event.get("payload") or {}
    dataset_key = str(payload.get("dataset_key") or "")
    event_id = str(event.get("event_id") or "")
    if not event_id:
        raise ValueError("WAL 事件缺 event_id，无法幂等入账")
    if _tier_of(db, customer_id, dataset_key, tiers) == "off":
        # off 档：跳过 append 但照常计入待 ack（由调用方推进 last_ok），
        # 引擎侧不留积压；调回非 off 档后仅对未来事件生效。
        stats.skipped_off += 1
        return
    replay = db.get(LedgerEvent, event_id) is not None
    append_event(
        db,
        ledger=customer_id,
        type=str(event.get("type") or "dataset.wal.event"),
        actor={
            "kind": "customer",
            "id": str(payload.get("actor") or "unknown"),
            "sig": None,  # 信封留位（D2 actor.sig，v2 不可否认门）
        },
        subject={"kind": "dataset", "key": dataset_key},
        payload=payload,  # 原样入账（摘要级负载，源全量留引擎侧按 id 引用）
        event_id=event_id,
    )
    db.commit()  # ack 前必须落账（见模块头注）；失败条目由调用方 rollback
    if not replay:
        stats.appended += 1


def _drain_customer_wal(
    db: Session, customer_id: str, client, stats: ForwardStats
) -> None:
    """拉干单客户的未 ack WAL：页循环直到空页/失败/护栏。

    - 本页全部处理成功才 ack 整页末位 seq；中途 append 异常则 ack 到
      最后成功条（下一轮重拉，event_id 幂等去重）；
    - ack 自身失败：本轮放弃该客户（已 commit 的入账不回滚，下一轮
      重拉重放，幂等保证零重复）。
    """
    tiers: dict[str, str] = {}
    for _ in range(_MAX_PAGES_PER_ROUND):
        try:
            page = client.call(
                TOOL_WAL_PENDING, {"after_seq": 0, "limit": _PAGE_LIMIT}
            )
        except Exception as exc:  # noqa: BLE001 - 引擎不可达/旧引擎无工具：跳过不抛
            stats.errors.append(
                f"customer {customer_id} wal_pending 失败（引擎不可达或旧引擎无 WAL 工具）: {exc}"
            )
            return
        events = page.get("events") or []
        if not events:
            return  # 已拉干（空页 last_seq 回落 after_seq，无新内容可 ack）
        last_ok = 0
        failed = False
        for event in events:
            try:
                _append_wal_event(db, customer_id, event, tiers, stats)
            except Exception as exc:  # noqa: BLE001 - 单条入账失败：停在最后成功条
                db.rollback()
                stats.errors.append(
                    f"customer {customer_id} WAL 事件 seq={event.get('seq')} "
                    f"({event.get('event_id')}) 入账失败: {exc}"
                )
                failed = True
                break
            last_ok = int(event.get("seq") or 0)
        if last_ok > 0:
            try:
                client.call(TOOL_WAL_ACK, {"upto_seq": last_ok})
                stats.acked_upto[customer_id] = last_ok
            except Exception as exc:  # noqa: BLE001 - ack 失败：下轮重拉重放（幂等）
                stats.errors.append(
                    f"customer {customer_id} wal_ack(upto={last_ok}) 失败: {exc}"
                )
                return
        else:
            return  # 首条即失败：不 ack，下一轮整页重拉
        if failed:
            return  # 已 ack 到最后成功条，失败条下一轮补入
    logger.warning(
        "customer %s 单轮 WAL 转发命中 %d 页护栏，剩余下轮继续",
        customer_id,
        _MAX_PAGES_PER_ROUND,
    )


def forward_once(db: Session) -> ForwardStats:
    """单发一轮：枚举客户 → 逐客户拉 WAL → 入账 → ack（绝不抛出）。

    事务边界归本函数（每条入账即时 commit，见模块头注）；``db`` 由调用
    方提供（后台循环自建会话；测试直传）。任何客户的任何失败都收敛为
    ``stats.errors`` 的一条记录，不中断其余客户。
    """
    stats = ForwardStats()
    factory = get_engine_client_factory()
    for customer_id in _forward_customers(db):
        stats.customers += 1
        try:
            client = factory(customer_id)
        except Exception as exc:  # noqa: BLE001 - 工厂失败与引擎失败同语义
            stats.errors.append(f"customer {customer_id} 引擎客户端构建失败: {exc}")
            continue
        try:
            _drain_customer_wal(db, customer_id, client, stats)
        except Exception as exc:  # noqa: BLE001 - 双保险：绝不抛出
            db.rollback()
            stats.errors.append(f"customer {customer_id} 转发失败: {exc}")
        finally:
            closer = getattr(client, "close", None)
            if closer is not None:
                with contextlib.suppress(Exception):
                    closer()
    return stats


# ------------------------------------------------------------- 后台轮询


def _run_once_with_own_session() -> ForwardStats:
    """轮询循环的运行体：自建 db 会话跑一轮（建表兜底幂等，生产走迁移）。"""
    from ..datasets.db import init_db
    from . import init_provenance_db

    init_db()
    init_provenance_db()
    db = get_session()
    try:
        return forward_once(db)
    finally:
        db.close()


async def _forwarder_loop() -> None:
    """轮询循环：间隔 env 可配（默认 30s），异常吞掉记日志等下一轮。

    先睡后跑（与 materialization 调度器同款）：启动瞬间的存量 WAL 等
    一个间隔再收，避免 lifespan 内抢跑。运行体是阻塞 IO（SQLAlchemy +
    httpx），经 ``asyncio.to_thread`` 下放线程，不卡事件循环。
    """
    logger.info("provenance WAL 转发循环启动（间隔 %ss）", _interval_seconds())
    while True:
        await asyncio.sleep(_interval_seconds())
        try:
            await asyncio.to_thread(_run_once_with_own_session)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 失败只记日志，下一轮重试
            logger.exception("provenance WAL 转发轮失败，下一间隔重试")


_forwarder_task: asyncio.Task | None = None


def start_forwarder_loop() -> asyncio.Task | None:
    """启动后台转发循环（幂等；需在运行中的事件循环内调用——app 生命周期接线）。

    无运行循环时记日志返回 None（不抛出——库导入场景不强制事件循环）。
    """
    global _forwarder_task
    if _forwarder_task is not None and not _forwarder_task.done():
        return _forwarder_task
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("无运行中的事件循环；provenance WAL 转发循环未启动")
        return None
    _forwarder_task = asyncio.create_task(_forwarder_loop())
    return _forwarder_task


async def stop_forwarder_loop() -> None:
    """停止后台转发循环并等待收尾（幂等；不泄漏 task）。"""
    global _forwarder_task
    task, _forwarder_task = _forwarder_task, None
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
