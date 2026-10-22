"""账本完整性自检（wire-provenance-ledger 任务 9.1）。

周期性（默认 24h）对全部账本执行三件事（与离线核验脚本
``fd-wire/scripts/evidence_verify.py`` 同口径，Merkle/哈希复用
``envelope``——唯一权威）：

1. 链连续：逐账本 ``ledger.verify_chain``（全链）；
2. 回执匹配：逐 ``ledger.epoch.sealed`` 回执重算区间 Merkle 根
   （叶 = 区间内非回执事件的 ``merkle_leaf(payload_hash)``）与回执
   ``payload.merkle_root`` 比对；
3. 区间构成：epoch 递增、区间首尾衔接不重叠不遗漏（首个封存区间自
   seq 1 起；相邻区间之间不得夹带未封存业务事件——回写回执除外）。

红线：**只读**。本模块绝不 append/UPDATE/DELETE、绝不 commit——
自检无修复（spec"账本完整性自检"）；数据库侧不可变触发器双保险。

告警接线：发现异常走既有日志通道
（``logging.getLogger("fd_wire_api.provenance").error``，由日志聚合
拾取）；控制台异常区间经 ``/overview`` 的 integrity 字段透出（账本
核心已实现，本模块不重复建设）。

循环：``start_selfcheck_loop()`` / ``stop_selfcheck_loop()``（asyncio，
间隔 env ``FD_WIRE_PROVENANCE_SELFCHECK_INTERVAL_SECONDS`` 默认 86400）。
接线归 app.py lifespan（主代理统一处理，本模块不自接线）。
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import envelope, ledger
from .models import LedgerEvent

#: 回执事件类型（与封存器/离线核验脚本一致；payload 含 epoch/first_seq/
#: last_seq/merkle_root/backend/external_ref/ts_external/algo）
RECEIPT_TYPE = "ledger.epoch.sealed"

#: 自检循环间隔 env 名 / 默认值（24h；spec 要求不低于每日）
SELFCHECK_INTERVAL_ENV = "FD_WIRE_PROVENANCE_SELFCHECK_INTERVAL_SECONDS"
SELFCHECK_INTERVAL_DEFAULT = 86400.0

logger = logging.getLogger("fd_wire_api.provenance")

#: 模块级最近一次报告（GET /api/v1/provenance/selfcheck 读取；进程内）
last_report: "SelfcheckReport | None" = None


@dataclass
class BrokenEntry:
    """单处异常定位：账本、原因、位置（链断裂 seq / 异常回执 seq）。"""

    ledger: str
    reason: str
    at: int | None = None

    def as_dict(self) -> dict:
        return {"ledger": self.ledger, "reason": self.reason, "at": self.at}


@dataclass
class SelfcheckReport:
    """自检结论（只读检查的结果快照，无修复动作）。"""

    checked_ledgers: int
    ok: bool
    broken: list[BrokenEntry] = field(default_factory=list)
    receipts_checked: int = 0
    receipts_bad: int = 0
    ran_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def as_dict(self) -> dict:
        return {
            "checked_ledgers": self.checked_ledgers,
            "ok": self.ok,
            "broken": [b.as_dict() for b in self.broken],
            "receipts_checked": self.receipts_checked,
            "receipts_bad": self.receipts_bad,
            "ran_at": self.ran_at.isoformat(),
        }


def _check_receipts(db: Session, ledger_name: str) -> tuple[list[BrokenEntry], int, int]:
    """单账本回执匹配 + 区间构成核验（只读）。

    返回 (broken 条目, 回执核验数, 不符回指数)。区间构成规则：
    - 回执自身 seq 晚于其区间末（回写在区间之后）；
    - 首个封存区间 first_seq == 1（头部无遗漏）；
    - 相邻区间 last < next.first（不重叠）且两区间之间只夹带回执事件
      （衔接不遗漏——跳过的业务事件即为遗漏）；
    - epoch 严格递增。
    """
    events = (
        db.execute(
            select(LedgerEvent)
            .where(LedgerEvent.ledger == ledger_name)
            .order_by(LedgerEvent.seq)
        )
        .scalars()
        .all()
    )
    receipts = [e for e in events if e.type == RECEIPT_TYPE]
    broken: list[BrokenEntry] = []
    if not receipts:
        return broken, 0, 0

    prev: LedgerEvent | None = None
    prev_first = prev_last = prev_epoch = None
    for rc in receipts:
        problems: list[str] = []
        p = rc.payload or {}
        first = p.get("first_seq")
        last = p.get("last_seq")
        claimed = p.get("merkle_root")
        epoch_no = p.get("epoch")

        if (
            not isinstance(first, int)
            or not isinstance(last, int)
            or not isinstance(epoch_no, int)
            or not isinstance(claimed, str)
            or not claimed
        ):
            broken.append(
                BrokenEntry(
                    ledger_name,
                    f"回执 {rc.id} 区间字段缺失/非法"
                    f"（epoch={epoch_no!r}, first_seq={first!r},"
                    f" last_seq={last!r}, merkle_root={claimed!r}）",
                    rc.seq,
                )
            )
            prev, prev_first, prev_last, prev_epoch = rc, None, None, None
            continue

        if rc.seq <= last:
            problems.append(
                f"回写位置先于区间末（回执 seq={rc.seq} ≤ last_seq={last}）"
            )

        leaves = [
            envelope.merkle_leaf(e.payload_hash)
            for e in events
            if first <= e.seq <= last and e.type != RECEIPT_TYPE
        ]
        actual = envelope.merkle_root(leaves)
        if actual != claimed:
            problems.append(
                f"epoch={epoch_no} 区间 [{first},{last}] merkle_root 不匹配"
                f"（回执 {str(claimed)[:16]}…，重算 {actual[:16]}…，"
                f"{len(leaves)} 叶）"
            )

        if prev is not None:
            if prev_first is not None:
                if first <= prev_last:
                    problems.append(
                        f"epoch={epoch_no} 区间 [{first},{last}] 与前一区间"
                        f" [{prev_first},{prev_last}] 重叠"
                    )
                else:
                    skipped = [
                        e.seq
                        for e in events
                        if prev_last < e.seq < first and e.type != RECEIPT_TYPE
                    ]
                    if skipped:
                        problems.append(
                            f"epoch={epoch_no} 区间衔接遗漏：seq {skipped} 的业务"
                            f"事件落在前一区间 [{prev_first},{prev_last}] 与本区间"
                            f" [{first},{last}] 之间未被封存"
                        )
                if epoch_no <= prev_epoch:
                    problems.append(
                        f"epoch 不递增（{prev_epoch} → {epoch_no}）"
                    )
        elif first != 1:
            problems.append(
                f"首个封存区间未自账本头部开始（first_seq={first}，应为 1，"
                f"头部事件遗漏封存）"
            )

        if problems:
            broken.append(BrokenEntry(ledger_name, "；".join(problems), rc.seq))
        prev, prev_first, prev_last, prev_epoch = rc, first, last, epoch_no

    bad = len(broken)
    return broken, len(receipts), bad


def run_selfcheck(db: Session) -> SelfcheckReport:
    """立即执行全量自检（**只读**：仅 SELECT，不 append/改写/commit）。

    覆盖全部账本：链连续 + 回执匹配 + 区间构成；结果写模块级
    ``last_report`` 供 GET 端点读取；发现异常时经既有日志通道告警。
    """
    ledger_names = list(db.execute(select(LedgerEvent.ledger).distinct()).scalars())

    broken: list[BrokenEntry] = []
    receipts_checked = receipts_bad = 0
    for name in ledger_names:
        result = ledger.verify_chain(db, name)
        if not result.ok:
            broken.append(
                BrokenEntry(name, result.reason or "链核验失败", result.broken_at)
            )
    for name in ledger_names:
        entries, checked, bad = _check_receipts(db, name)
        broken.extend(entries)
        receipts_checked += checked
        receipts_bad += bad

    report = SelfcheckReport(
        checked_ledgers=len(ledger_names),
        ok=not broken,
        broken=broken,
        receipts_checked=receipts_checked,
        receipts_bad=receipts_bad,
        ran_at=datetime.now(timezone.utc),
    )
    global last_report
    last_report = report
    if broken:
        logger.error(
            "账本完整性自检发现异常：%d 个异常点，%d/%d 回执不符：%s",
            len(broken),
            receipts_bad,
            receipts_checked,
            "; ".join(
                f"[{b.ledger}] seq={b.at} {b.reason}" for b in broken
            ),
        )
    else:
        logger.info(
            "账本完整性自检通过：%d 个账本，%d 条回执全部匹配",
            len(ledger_names),
            receipts_checked,
        )
    return report


def get_last_report() -> SelfcheckReport | None:
    """最近一次自检报告（进程内；尚未运行过 → None）。"""
    return last_report


def reset_selfcheck_runtime() -> None:
    """清模块态（last_report/循环任务）——测试隔离用。"""
    global _loop_task, last_report
    if _loop_task is not None and not _loop_task.done():
        _loop_task.cancel()
    _loop_task = None
    last_report = None


# ------------------------------------------------------------- 周期循环

_loop_task: "asyncio.Task[None] | None" = None


def _interval_seconds() -> float:
    raw = os.getenv(SELFCHECK_INTERVAL_ENV, "")
    try:
        value = float(raw) if raw else SELFCHECK_INTERVAL_DEFAULT
    except ValueError:
        value = SELFCHECK_INTERVAL_DEFAULT
    return value if value > 0 else SELFCHECK_INTERVAL_DEFAULT


def _run_once() -> SelfcheckReport:
    """单次自检（工作线程内执行：自建会话，只读账本）。"""
    from ..datasets.db import get_session
    from . import init_provenance_db

    init_provenance_db()  # 幂等兜底（生产走迁移链时短路）
    db = get_session()
    try:
        return run_selfcheck(db)
    finally:
        db.close()


async def _selfcheck_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(_run_once)
        except Exception:  # noqa: BLE001 —— 循环体永不因单次失败退出
            logger.exception("账本完整性自检运行异常（下个周期重试）")
        await asyncio.sleep(_interval_seconds())


def start_selfcheck_loop() -> "asyncio.Task[None]":
    """启动周期自检循环（幂等；须在事件循环内调用——app lifespan 接线）。

    启动即跑一次，此后每 ``FD_WIRE_PROVENANCE_SELFCHECK_INTERVAL_SECONDS``
    秒（默认 86400 = 每日，spec 下限）一次。
    """
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        return _loop_task
    _loop_task = asyncio.ensure_future(_selfcheck_loop())
    return _loop_task


def stop_selfcheck_loop() -> "asyncio.Task[None] | None":
    """停止循环（取消任务；返回被取消的任务供调用方按需 await）。"""
    global _loop_task
    task, _loop_task = _loop_task, None
    if task is not None and not task.done():
        task.cancel()
    return task
