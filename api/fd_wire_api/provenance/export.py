"""证据包导出（wire-provenance-ledger 任务 7.1）。

证据包 = 客户自持的完整账本快照，布局与离线核验脚本
``fd-wire/scripts/evidence_verify.py``（格式权威）逐字对齐::

    ledger.jsonl                # 该账本全部事件（每行一个 JSON 对象 =
                                #   完整信封 dict + event_hash + created_at）
    checkpoints/epoch-<N>.json  # 客户自持检查点（anchor-customer 模式，
                                #   配置了 checkpoint_dir 且落盘时全部拷入）
    anchors/                    # 外部锚定凭据（OTS 文件等）将来落盘归此处，
                                #   当前仅建目录 + README 说明

明文负载说明（设计允许）：``ledger.jsonl`` 每行携带完整 ``payload``。
"链上零明文"红线约束的是**锚定后端提交到外部的**内容（仅哈希与最小
元数据）；证据包是客户自己的数据交付客户自己，包内明文不越出客户
信任边界。离线核验脚本亦按"信封含 payload"的口径重算
payload_hash/event_hash（见其 ``_ENVELOPE_HASH_FIELDS`` 与哈希口径注释）。

只读账本：本模块仅 SELECT 与文件写，绝不 append/UPDATE/DELETE。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import envelope
from .models import LedgerEvent
from .settings import get_settings

_ANCHORS_README = (
    "外部锚定凭据（OpenTimestamps .ots 等）落盘后归本目录。\n"
    "离线核验脚本（evidence_verify.py）不联网复核，仅列出供人工/工具复核。\n"
)


def checkpoint_dir_setting() -> str:
    """防御式读取 ``checkpoint_dir``（并行封存器任务可能尚未落该字段）。

    缺字段/空串 = 未配置客户自持检查点目录，证据包的 checkpoints/ 留空。
    """
    return str(getattr(get_settings(), "checkpoint_dir", "") or "")


def _envelope_line(row: LedgerEvent) -> str:
    """账本行 → ledger.jsonl 单行 JSON（信封全字段 + event_hash + created_at）。

    核验脚本按 ``envelope.envelope_from_row`` 同构字段重算比对：列名即行
    属性名（id/ledger/seq/…），``v`` 代入当前表世代，``created_at`` 不参与
    event_hash（离线侧哈希基排除该字段，两侧口径一致）。
    """
    env = envelope.envelope_from_row(row)
    env["event_hash"] = row.event_hash
    env["created_at"] = row.created_at.isoformat() if row.created_at else None
    return json.dumps(env, ensure_ascii=False, separators=(",", ":"))


def checkpoint_path(ledger: str, epoch: int) -> Path | None:
    """客户自持检查点文件路径（未配置 checkpoint_dir 或文件不存在 → None）。

    目录约定：``<checkpoint_dir>/<ledger>/epoch-<N>.json``（与封存器任务
    的落盘布局一致；导出与单文件下载共用本入口）。
    """
    root = checkpoint_dir_setting()
    if not root:
        return None
    return Path(root) / ledger / f"epoch-{epoch}.json"


def build_evidence_bundle(db: Session, ledger: str, out_dir: Path) -> Path:
    """在 ``out_dir`` 组装该账本的完整证据包（只读账本；返回 ``out_dir``）。

    - ``ledger.jsonl``：该账本全部事件按 seq 升序逐行写出；
    - ``checkpoints/``：``checkpoint_dir/<ledger>/epoch-*.json`` 全部拷入
      （未配置/无文件则目录留空——如实导出，不伪造检查点）；
    - ``anchors/``：建目录 + README 一行（OTS 凭据将来落盘归此处）。
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = (
        db.execute(
            select(LedgerEvent)
            .where(LedgerEvent.ledger == ledger)
            .order_by(LedgerEvent.seq)
        )
        .scalars()
        .all()
    )
    with (out_dir / "ledger.jsonl").open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(_envelope_line(row) + "\n")

    checkpoints_dir = out_dir / "checkpoints"
    checkpoints_dir.mkdir(exist_ok=True)
    root = checkpoint_dir_setting()
    if root:
        source_dir = Path(root) / ledger
        if source_dir.is_dir():
            for cp_file in sorted(source_dir.glob("epoch-*.json")):
                shutil.copy2(cp_file, checkpoints_dir / cp_file.name)

    anchors_dir = out_dir / "anchors"
    anchors_dir.mkdir(exist_ok=True)
    (anchors_dir / "README.txt").write_text(_ANCHORS_README, encoding="utf-8")
    return out_dir
