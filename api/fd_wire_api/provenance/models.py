"""provenance 切片 ORM 模型（wire-provenance-ledger D1/D2，任务 2.1）。

``ledger_events`` 单表即平台侧溯源账本：信封全字段 + 入库时间。账本按
企业划分（``ledger`` = customer_id），``seq`` 企业内单调（uq(ledger, seq)
兜底并发追加）。列与 Alembic 迁移 0012 逐列一致——``create_all`` 仅为
开发兜底，生产走迁移链；不可变（append-only）由 BEFORE UPDATE/DELETE
触发器强制（触发器 DDL 见 ``__init__.init_provenance_db`` 与迁移 0012
的双保险说明：应用层 REVOKE + 触发器，任何直连会话也改不动已入账事件）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..datasets.db import DatasetsBase


def _utcnow_naive() -> datetime:
    """入库时间（UTC 无时区，与 datasets 切片同款纪律）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class LedgerEvent(DatasetsBase):
    """账本事件行（append-only；信封哈希可由 ``envelope`` 从行字段复算）。

    信封版本 ``v`` 不落列：本表世代恒为 ``envelope.EVENT_VERSION``，
    复算即代入（迁移 0012 注释"v 内嵌于信封哈希可复算性"）。
    """

    __tablename__ = "ledger_events"
    __table_args__ = (
        UniqueConstraint("ledger", "seq", name="uq_ledger_events_ledger_seq"),
    )

    #: 事件 id（``evt_<uuid4hex>``；跨系统转发按此幂等去重）
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    #: 账本名 = customer_id（D1：账本按企业划分）
    ledger: Mapped[str] = mapped_column(String(64), index=True)
    #: 企业内单调序号（并发冲突由 uq(ledger, seq) 兜底）
    seq: Mapped[int] = mapped_column(Integer)
    #: 命名空间化事件类型（dataset.* / cell.* 起步；新事件类型不改表结构）
    type: Mapped[str] = mapped_column(String(100), index=True)
    #: 平台时间（UTC ISO-8601 秒级带 Z；入信封、入哈希）
    ts_platform: Mapped[str] = mapped_column(String(40))
    #: 操作者 {"kind": "customer|engine|platform", "id": ..., "sig": null}
    actor: Mapped[dict] = mapped_column(JSON)
    #: 事件主体（如 {"kind": "dataset", "key": "sales_2026"}）
    subject: Mapped[dict] = mapped_column(JSON)
    #: 摘要级负载（新旧值全量留源表按 id 引用——链上零明文，见 D2/D5）
    payload: Mapped[dict] = mapped_column(JSON)
    #: 负载哈希 sha256:<hex>（对负载规范化 JSON）
    payload_hash: Mapped[str] = mapped_column(String(80))
    #: 随机数（16B hex，防小值域彩虹表枚举）
    nonce: Mapped[str] = mapped_column(String(40))
    #: 前一事件哈希（账本首条 = 创世哈希 sha256:000…0）
    prev_hash: Mapped[str] = mapped_column(String(80))
    #: 算法敏捷位（v1 恒 "sha256"）
    algo: Mapped[str] = mapped_column(String(16))
    #: 本事件哈希（对不含 event_hash/created_at 的信封规范化 JSON 哈希）
    event_hash: Mapped[str] = mapped_column(String(80))
    #: 入库时间（不参与 event_hash）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive)
