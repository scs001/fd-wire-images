"""溯源账本端点：客户面 + 内部入账（wire-provenance-ledger 任务组 2；
wire-self-serve-launch 4.5 客户面身份切换）。

- 客户面（Bearer ID token → logto_sub → Customer，与 datasets 切片同款
  身份依赖，缺失/未映射 401；X-Customer-Id 占位头退位不再接受）：
  ``/overview`` 企业概览、``/datasets/{key}/events`` 时间线（倒序分页）、
  ``/datasets/{key}/status`` 溯源状态、``/datasets/{key}/tier`` 调档。
- 内部面：``POST /ingest`` 供 cell-manager/转发器远端入账
  （``FD_WIRE_PROVENANCE_INGEST_TOKEN`` 非空时要求 ``X-Provenance-Token``
  常量时间比对匹配；空 = 开发态不设防）。
- 证据包（任务 7.1）：``GET /evidence-package`` 全量 zip 导出、
  ``GET /checkpoints/{epoch}``` 客户自持检查点单文件下载。
- 完整性自检（任务 9.1）：``GET /selfcheck`` 最近报告、
  ``POST /selfcheck/run`` 立即执行（admin 侧语义，暂挂客户鉴权）。

锚定进度字段在任务组 5（封存器/锚定后端）落地前如实返回：未封存
（None）+ 全量滞后计数——绝不假装已锚定。
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import tempfile
import zipfile
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from ..console.auth import require_console_customer_id
from ..datasets.db import get_session
from ..datasets.models import Dataset
from . import envelope, export, ledger, selfcheck
from .models import LedgerEvent
from .sealing import RECEIPT_TYPE
from .schemas import (
    AnchoringOut,
    DatasetStatusOut,
    EventEnvelopeOut,
    IngestIn,
    IntegrityOut,
    OverviewOut,
    TierIn,
    TierOut,
    TimelineEventOut,
)
from .settings import get_settings

router = APIRouter(tags=["provenance"])


def require_ingest_token(
    x_provenance_token: str | None = Header(default=None, alias="X-Provenance-Token"),
) -> None:
    """内部入账令牌：``ingest_token`` 非空时常量时间比对（学 require_internal）。"""
    expected = get_settings().ingest_token
    if expected and (
        not x_provenance_token
        or not secrets.compare_digest(x_provenance_token, expected)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="ingest 端点需要有效的 X-Provenance-Token",
        )


def get_provenance_db() -> Iterator[Session]:
    """请求会话：先确保账本表 + 不可变触发器就绪（create_all 兜底模式）。

    与 datasets 同库同会话工厂；``init_provenance_db`` 幂等（标记位短路）。
    """
    from . import init_provenance_db  # 延迟导入：包 __init__ 亦导入本模块

    init_provenance_db()
    db = get_session()
    try:
        yield db
    finally:
        db.close()


def _dataset_or_404(db: Session, key: str, customer_id: str) -> Dataset:
    """数据集定位（customer + key；跨企业一律 404，与 datasets 详情同款纪律）。"""
    dataset = db.execute(
        select(Dataset).where(Dataset.customer_id == customer_id, Dataset.key == key)
    ).scalar_one_or_none()
    if dataset is None:
        raise HTTPException(status_code=404, detail=f"数据集 {key} 不存在")
    return dataset


def _last_seq(db: Session, ledger: str) -> int:
    return int(
        db.execute(
            select(func.coalesce(func.max(LedgerEvent.seq), 0)).where(
                LedgerEvent.ledger == ledger
            )
        ).scalar_one()
    )


# ------------------------------------------------------------- 企业概览


@router.get("/api/v1/provenance/overview", response_model=OverviewOut)
def provenance_overview(
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_provenance_db),
) -> OverviewOut:
    """企业溯源概览：账本末位 seq、链完整性、锚定进度（滞后如实计数）。"""
    last_seq = _last_seq(db, customer_id)
    verify = ledger.verify_chain(db, customer_id)
    last_receipt = _last_receipt(db, customer_id)
    sealed_through = last_receipt["last_seq"] if last_receipt else 0
    return OverviewOut(
        ledger=customer_id,
        last_seq=last_seq,
        integrity=IntegrityOut(ok=verify.ok, checked=verify.checked),
        anchoring=AnchoringOut(
            backend=get_settings().anchor_backend,
            last_sealed_epoch=last_receipt["epoch"] if last_receipt else None,
            lag_events=max(0, last_seq - sealed_through),
        ),
    )


def _last_receipt(db: Session, customer_id: str) -> dict | None:
    """账本最新一条回执事件的 payload（无回执返回 None）。

    封存器（sealing.seal_ledger）落地的回填点：锚定进度即由此推导——
    last_sealed_epoch=回执 epoch，滞后=last_seq-回执区间末。
    """
    row = (
        db.query(LedgerEvent)
        .filter(LedgerEvent.ledger == customer_id, LedgerEvent.type == RECEIPT_TYPE)
        .order_by(LedgerEvent.seq.desc())
        .first()
    )
    return row.payload if row is not None else None


# ------------------------------------------------------------- 时间线 / 状态


@router.get(
    "/api/v1/provenance/datasets/{key}/events",
    response_model=list[TimelineEventOut],
)
def dataset_events(
    key: str,
    before_seq: int | None = Query(default=None, ge=1, description="取 seq 小于该值的事件（向上翻页）"),
    limit: int = Query(default=50, ge=1, le=500),
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_provenance_db),
) -> list[TimelineEventOut]:
    """数据集事件时间线：仅 subject 指向本数据集的事件，seq 倒序分页。"""
    _dataset_or_404(db, key, customer_id)
    stmt = select(LedgerEvent).where(
        LedgerEvent.ledger == customer_id,
        LedgerEvent.subject["kind"].as_string() == "dataset",
        LedgerEvent.subject["key"].as_string() == key,
    )
    if before_seq is not None:
        stmt = stmt.where(LedgerEvent.seq < before_seq)
    stmt = stmt.order_by(LedgerEvent.seq.desc()).limit(limit)
    rows = db.execute(stmt).scalars().all()
    return [TimelineEventOut.from_row(row) for row in rows]


@router.get(
    "/api/v1/provenance/datasets/{key}/status", response_model=DatasetStatusOut
)
def dataset_status(
    key: str,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_provenance_db),
) -> DatasetStatusOut:
    """数据集溯源状态：档位 + 时间线事件哈希自洽 + 溯源起点如实标注。

    完整链连续性（跨数据集）以 ``/overview`` 的全链核验为准；本端点对
    该数据集的事件逐条复算 event_hash（时间线内自洽）。
    """
    dataset = _dataset_or_404(db, key, customer_id)
    rows = (
        db.execute(
            select(LedgerEvent)
            .where(
                LedgerEvent.ledger == customer_id,
                LedgerEvent.subject["kind"].as_string() == "dataset",
                LedgerEvent.subject["key"].as_string() == key,
            )
            .order_by(LedgerEvent.seq)
        )
        .scalars()
        .all()
    )
    ok = all(
        envelope.event_hash(envelope.envelope_from_row(row)) == row.event_hash
        for row in rows
    )
    last_receipt = _last_receipt(db, customer_id)
    return DatasetStatusOut(
        tier=dataset.provenance_tier,
        integrity=IntegrityOut(ok=ok, checked=len(rows)),
        anchored_through=(
            {"epoch": last_receipt["epoch"], "backend": last_receipt["backend"]}
            if last_receipt
            else None
        ),
        first_event_ts=rows[0].ts_platform if rows else None,
        note=None if rows else "启用前无历史事件（溯源起点=启用时点）",
    )


# ------------------------------------------------------------- 调档


@router.post("/api/v1/provenance/datasets/{key}/tier", response_model=TierOut)
def set_dataset_tier(
    key: str,
    payload: TierIn,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_provenance_db),
) -> TierOut:
    """调档：写 ``datasets.provenance_tier``，即时生效于后续事件。

    已入账事件不因调档失效或被清除（契约"溯源档位"）；调档本身不入账
    （档位是配置不是客户数据事件）。
    """
    dataset = _dataset_or_404(db, key, customer_id)
    dataset.provenance_tier = payload.tier
    db.commit()
    db.refresh(dataset)
    return TierOut(key=dataset.key, tier=dataset.provenance_tier)


# ------------------------------------------------------------- 内部入账


@router.post("/api/v1/provenance/ingest", response_model=EventEnvelopeOut)
def ingest_event(
    payload: IngestIn,
    _token: None = Depends(require_ingest_token),
    db: Session = Depends(get_provenance_db),
) -> EventEnvelopeOut:
    """内部入账（cell-manager 生命周期事件 / 转发器 WAL 事件 → 平台账本）。

    幂等：同 ``event_id`` 重复提交返回既有信封（seq 不变、不重复入账）。
    """
    row = ledger.append_event(
        db,
        ledger=payload.ledger,
        type=payload.type,
        actor=payload.actor,
        subject=payload.subject,
        payload=payload.payload,
        event_id=payload.event_id,
    )
    db.commit()
    return EventEnvelopeOut.from_row(row)


# ------------------------------------------------------------- 证据包导出（任务 7.1）

_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename_part(value: str) -> str:
    """Content-Disposition 文件名安全化（客户 id 等不可信段只留通行字符）。"""
    cleaned = _FILENAME_SAFE.sub("_", value).strip("._-") or "ledger"
    return cleaned[:64]


@router.get("/api/v1/provenance/evidence-package")
def export_evidence_package(
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_provenance_db),
) -> FileResponse:
    """导出本企业完整证据包（zip 下载；离线核验脚本可直接消费）。

    包内 = 该账本（= customer_id）全部事件的 ``ledger.jsonl`` + 检查点
    （如配置）+ anchors/ 说明，布局见 ``provenance/export.py``。明文
    payload 留在事件行内进包是**设计允许**的：证据包是客户自持的自己
    的完整数据（脱离平台后仍可独立核验），“链上零明文”红线只约束
    锚定后端**提交到外部**的内容（仅哈希与最小元数据），不适用于
    客户自持的完整包。

    实现：临时目录组包 → stdlib zipfile 压缩为临时文件 → FileResponse
    流式发送，后台任务清理临时文件（请求结束前文件存在）。
    """
    bundle_dir = Path(tempfile.mkdtemp(prefix="fd-wire-evidence-"))
    export.build_evidence_bundle(db, customer_id, bundle_dir)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    zip_name = (
        f"evidence-package-{_safe_filename_part(customer_id)}-{stamp}.zip"
    )
    fd, zip_path = tempfile.mkstemp(prefix="fd-wire-evidence-", suffix=".zip")
    os.close(fd)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(bundle_dir.rglob("*")):
            arcname = path.relative_to(bundle_dir).as_posix()
            zf.write(path, arcname + ("/" if path.is_dir() else ""))

    def _cleanup() -> None:
        shutil.rmtree(bundle_dir, ignore_errors=True)
        Path(zip_path).unlink(missing_ok=True)

    return FileResponse(
        zip_path,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
        background=BackgroundTask(_cleanup),
    )


@router.get("/api/v1/provenance/checkpoints/{epoch}")
def download_checkpoint(
    epoch: int,
    customer_id: str = Depends(require_console_customer_id),
) -> FileResponse:
    """客户自持检查点单文件下载（``epoch-<N>.json``；不存在 404）。

    目录约定：``<checkpoint_dir>/<ledger>/epoch-<epoch>.json``（与证据包
    checkpoints/ 同源；未配置 checkpoint_dir 时恒 404——如实，不伪造）。
    """
    if epoch < 1:
        raise HTTPException(status_code=404, detail=f"epoch 非法：{epoch}")
    path = export.checkpoint_path(customer_id, epoch)
    if path is None or not path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"检查点 epoch-{epoch}.json 不存在（未配置或未封存）",
        )
    return FileResponse(
        path,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="epoch-{epoch}.json"'
        },
    )


# ------------------------------------------------------------- 完整性自检（任务 9.1）


@router.get("/api/v1/provenance/selfcheck")
def get_selfcheck_report(customer_id: str = Depends(require_console_customer_id)) -> dict:
    """最近一次账本完整性自检报告（尚未运行过 → 404）。

    admin 侧语义：生产应挂 admin 鉴权（如 ``admin.auth.require_admin_token``
    的 X-Admin-Token 模式）；当前切片内无 admin 体系，暂与 /overview 同款
    客户骨架鉴权，不引入新鉴权体系——报告为平台级只读结论。
    """
    report = selfcheck.get_last_report()
    if report is None:
        raise HTTPException(
            status_code=404, detail="尚未运行过自检（POST selfcheck/run 触发）"
        )
    return report.as_dict()


@router.post("/api/v1/provenance/selfcheck/run")
def run_selfcheck_now(
    customer_id: str = Depends(require_console_customer_id),  # noqa: ARG001 —— 鉴权占位（同上）
    db: Session = Depends(get_provenance_db),
) -> dict:
    """立即执行一次全量自检并返回报告（只读：不 append/改写/commit）。

    覆盖全部账本的链连续 + 回执匹配 + 区间构成；发现异常走既有日志通道
    告警。admin 侧语义与鉴权说明同 GET /selfcheck。周期调度由
    ``selfcheck.start_selfcheck_loop`` 在 app lifespan 接线（另任务）。
    """
    return selfcheck.run_selfcheck(db).as_dict()
