"""epoch 封存器（wire-provenance-ledger D4，任务 5.1/5.5/5.6）。

封存一轮 = 找未封存区间 → 区间 Merkle 根（叶子 = ``payload_hash`` 双
SHA-256，权威口径 = ``envelope.merkle_leaf/merkle_root``，与
``fd-wire/scripts/evidence_verify.py`` 逐字等价）→ 提交锚定后端拿回执
→ 回执作为紧随区间的事件回写链上（``ledger.epoch.sealed``）。

不变式：

- **绝不半封存**——锚定后端失败即抛出、不回写任何回执（本轮作废，
  下轮对同一区间重算重提交；区间根由账本内容确定性导出，重试幂等）。
- **回执自身入链**——伪造/替换/冒充回执在链结构（prev_hash 链）与
  区间重算（``interval_root`` ≠ 回执 root）两个面上都可检知。
- **链上零明文**——回执 payload 只含 epoch/区间/根/后端标识/外部凭证
  引用/时间/算法位，绝无业务负载值。

哈希与字段名与 ``evidence_verify.py`` 的核验读取严格一致，改一处必改另一处。
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import envelope, ledger
from .anchors import AnchorBackend, get_backend
from .models import LedgerEvent
from .settings import get_settings

logger = logging.getLogger(__name__)

#: 回执回写事件类型（与 evidence_verify.py 的 RECEIPT_TYPE 一致）
RECEIPT_TYPE = "ledger.epoch.sealed"

#: 回执事件的操作者/主体（平台封存器身份；D2 的 actor 命名空间）
_SEALER_ACTOR = {"kind": "platform", "id": "sealer"}


def _last_receipt(db: Session, ledger_name: str) -> LedgerEvent | None:
    """最近一条回执事件（按 seq 降序首条）；无封存史返回 None。"""
    return (
        db.execute(
            select(LedgerEvent)
            .where(
                LedgerEvent.ledger == ledger_name,
                LedgerEvent.type == RECEIPT_TYPE,
            )
            .order_by(LedgerEvent.seq.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )


def _interval_events(db: Session, ledger_name: str, first: int, last: int) -> list[LedgerEvent]:
    """区间内参与 Merkle 构成的事件（回执事件不含——与 evidence_verify 口径一致）。"""
    return list(
        db.execute(
            select(LedgerEvent)
            .where(
                LedgerEvent.ledger == ledger_name,
                LedgerEvent.seq >= first,
                LedgerEvent.seq <= last,
                LedgerEvent.type != RECEIPT_TYPE,
            )
            .order_by(LedgerEvent.seq)
        )
        .scalars()
    )


def interval_root(db: Session, ledger_name: str, first: int, last: int) -> str:
    """重算区间 Merkle 根（自检/回执对账用；权威 = envelope.merkle_root）。

    回执冒充检测 = 本函数重算值 ≠ 回执 payload 声称的 merkle_root 即检知。
    """
    return envelope.merkle_root(
        [envelope.merkle_leaf(e.payload_hash) for e in _interval_events(db, ledger_name, first, last)]
    )


def seal_ledger(
    db: Session,
    ledger_name: str,
    *,
    realtime: bool = False,
    backend: AnchorBackend | None = None,
) -> dict | None:
    """封存一个账本的未封存区间，返回回执 payload（空区间返回 None）。

    步骤（D4）：上一回执 ``payload.last_seq``（无则 0）+1 到当前 max(seq)
    为未封存区间；区间事件（不含回执）payload_hash 列表 → Merkle 根；
    epoch 号 = 上一回执 epoch+1（无则 1）；提交锚定后端拿回执字段；
    回执作为紧随区间的事件回写链上并提交事务。

    - 后端失败 → 异常上抛、**本轮不封存**（无任何回执/状态写入，下轮重试）；
    - ``realtime=True`` 供 anchored_realtime 档即时调用（不等周期；封存的
      就是当前全部待封事件——"事件到达即提交单事件根"，语义由调用方保证）；
    - ``backend`` 可注入后端（测试/即时档），缺省取 ``settings.anchor_backend``。
    """
    backend = backend or get_backend(get_settings().anchor_backend)

    last_receipt = _last_receipt(db, ledger_name)
    if last_receipt is None:
        prev_epoch, covered_upto = 0, 0
    else:
        payload = last_receipt.payload or {}
        prev_epoch, covered_upto = payload.get("epoch"), payload.get("last_seq")
        if not isinstance(prev_epoch, int) or not isinstance(covered_upto, int):
            raise ValueError(
                f"回执事件 {last_receipt.id} 的 payload 缺失/非法 epoch/last_seq"
                "（账本疑似被污染，拒绝继续封存）"
            )

    current_max = int(
        db.execute(
            select(func.coalesce(func.max(LedgerEvent.seq), 0)).where(
                LedgerEvent.ledger == ledger_name
            )
        ).scalar_one()
    )
    first, last = covered_upto + 1, current_max
    if first > last:
        return None  # 空区间（账本无事件或已全部封存）
    interval_events = _interval_events(db, ledger_name, first, last)
    if not interval_events:
        # 区间内只剩回执事件（上一轮回写把 max(seq) 顶高了一位）：
        # 无业务事件可封 → 视为空区间，不产生空叶 epoch（空叶根无锚定意义）
        return None

    epoch = prev_epoch + 1
    root = envelope.merkle_root(
        [envelope.merkle_leaf(e.payload_hash) for e in interval_events]
    )
    ts = envelope.utc_now_iso_z()
    # 后端失败在此抛出——尚未 append 任何事件，本轮零写入（绝不半封存）
    submitted = backend.submit(
        ledger_name,
        root,
        {"epoch": epoch, "first_seq": first, "last_seq": last, "ts": ts, "realtime": realtime},
    )

    receipt_payload = {
        "epoch": epoch,
        "first_seq": first,
        "last_seq": last,
        "merkle_root": root,
        # 后端回执字段（backend/external_ref/ts_external + 后端额外字段）
        **{k: v for k, v in submitted.items() if k not in ("epoch", "first_seq", "last_seq", "merkle_root")},
        "algo": envelope.HASH_ALGO,
    }
    receipt = ledger.append_event(
        db,
        ledger=ledger_name,
        type=RECEIPT_TYPE,
        actor=dict(_SEALER_ACTOR),
        subject={"kind": "ledger", "id": ledger_name},
        payload=receipt_payload,
    )
    db.commit()
    logger.info(
        "封存 ledger=%s epoch=%s 区间 [%d,%d] backend=%s 回执 seq=%d%s",
        ledger_name, epoch, first, last, receipt_payload.get("backend"),
        receipt.seq, "（realtime）" if realtime else "",
    )
    return dict(receipt_payload)


def seal_all(
    db: Session, *, realtime: bool = False, backend: AnchorBackend | None = None
) -> dict[str, dict | None]:
    """遍历有事件的账本逐个封存（空区间跳过；单账本失败不阻断其余）。

    返回 ``{ledger: 回执 payload | None | {"error": str}}``——None = 空区间
    跳过，error = 该账本轮失败（下轮重试）。
    """
    ledgers = [
        row for row in db.execute(
            select(LedgerEvent.ledger).distinct().order_by(LedgerEvent.ledger)
        ).scalars()
    ]
    results: dict[str, dict | None] = {}
    for ledger_name in ledgers:
        try:
            results[ledger_name] = seal_ledger(db, ledger_name, realtime=realtime, backend=backend)
        except Exception as exc:  # 单账本失败：记录并继续（本轮不封存该账本）
            logger.exception("封存失败 ledger=%s（本轮跳过，下轮重试）", ledger_name)
            results[ledger_name] = {"error": f"{type(exc).__name__}: {exc}"}
    return results


# ------------------------------------------------------------- 后台封存循环

_seal_task: asyncio.Task | None = None


async def _sealer_loop(interval_seconds: float | None = None) -> None:
    """周期封存循环：间隔 = epoch_hours 小时（测试可注入秒）。

    ``anchor_backend == none``（chain_only 世界）时空转——不触库不封存，
    但每轮重读配置（改配置无需重启进程即可生效）。
    """
    from ..datasets.db import get_session  # 延迟导入：与请求路径解耦

    while True:
        try:
            if get_settings().anchor_backend != "none":
                db = get_session()
                try:
                    seal_all(db)
                finally:
                    db.close()
        except Exception:
            logger.exception("封存周期执行失败（下轮重试）")
        interval = (
            interval_seconds
            if interval_seconds is not None
            else get_settings().epoch_hours * 3600.0
        )
        await asyncio.sleep(interval)


def start_sealer_loop(interval_seconds: float | None = None) -> asyncio.Task:
    """启动封存循环（幂等：已在跑直接返回既有任务）。

    ``interval_seconds`` 仅供测试注入秒级间隔；生产缺省 ``epoch_hours`` 小时。
    app.py 接线由主代理负责——本模块不动应用装配。
    """
    global _seal_task
    if _seal_task is not None and not _seal_task.done():
        return _seal_task
    _seal_task = asyncio.ensure_future(_sealer_loop(interval_seconds))
    return _seal_task


def stop_sealer_loop() -> None:
    """停止封存循环（取消后台任务；未启动时是安全空操作）。

    取消后任务对象的收殓（await CancelledError）归调用方/事件循环。
    """
    global _seal_task
    task, _seal_task = _seal_task, None
    if task is not None and not task.done():
        task.cancel()
