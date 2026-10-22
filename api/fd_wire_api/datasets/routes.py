"""客户数据集客户面端点（契约 §3.3；wire-self-serve-launch 4.5 身份切换）。

客户面（Bearer ID token → logto_sub → Customer，缺失/未映射 401；跨企业
一律 404；X-Customer-Id 占位头退位不再接受）：
- 列表/详情/删除（cell 层删除调引擎 ``delete``，引擎不可达即 502
  fail-closed；api 层删除走本地行存储，零引擎调用）
- 上传（multipart：CSV/Excel 解析 + 保守类型推断 + 样例预览 + 上限拒收）
- 建档（tier 判定在建集时刻按客户档位：免费档→api 层本地物化、付费档→
  cell 引擎 ingest；wire-free-tier-data-gate D1/D4）
- 行预览（cell 层透传引擎 ``preview``；api 层本地执行）、修正三件套
  （cell 层透传引擎覆盖层；api 层走本地修正层——wire-dataset-form-editing，
  基行永不改写）、同步历史、漂移恢复 ``resume``
- 增量同步设置 ``PATCH /api/v1/datasets/{id}/sync-settings``
  （wire-connector-incremental-sync；仅连接器数据集：模式切换、水位列
  ∈ columns_json、重置水位；切回 snapshot 清水位两字段）
- CSV 导出 ``GET /api/v1/datasets/{id}/export``（wire-dataset-export）：
  api 层本地有效视图全量；cell 层引擎 query 分页循环（1000/页 × 10 页
  上限），触顶 ``X-Export-Truncated`` 如实标注；UTF-8 带 BOM
- 连接器管理：配对令牌签发、列表（心跳推导状态）、改名、吊销
- 配额：``GET /api/v1/datasets/quota``（免费档真实数字；付费档 max=null）

纪律：cell 层对 cell 数据的一切读写都经 ``ingest.EngineToolClient``
（平台服务凭据轨道），不存在直接打开 daas.db/数据卷的路径（契约 §6.5）；
api 层数据集（免费档）全链路不触碰 EngineToolClient。
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import api_store, service, uploads
from .db import get_db
from .ingest import (
    EngineCallError,
    EngineClientFactory,
    EngineUnavailable,
    TOOL_CORRECTION_ADD,
    TOOL_CORRECTION_LIST,
    TOOL_CORRECTION_REVERT,
    TOOL_CREATE_DECLARED,
    TOOL_DELETE,
    TOOL_INGEST_ABORT,
    TOOL_INGEST_BEGIN,
    TOOL_INGEST_CHUNK,
    TOOL_INGEST_COMMIT,
    TOOL_PREVIEW,
    TOOL_QUERY,
    get_engine_client_factory,
)
from .models import Connector, ConnectorKey, ConnectorPairingToken, Dataset, DatasetSync, Upload
from .schemas import (
    ColumnIn,
    ConnectorOut,
    ConnectorPatchIn,
    CorrectionIn,
    CorrectionOut,
    DatasetCreateIn,
    DatasetOut,
    DatasetQuotaOut,
    PairingTokenOut,
    ResumeIn,
    SyncOut,
    SyncSettingsPatchIn,
    UploadOut,
)
from .settings import get_settings
from ..ai.db import get_db as get_ai_db
from ..console.auth import require_console_customer_id
from ..provenance.ledger import append_event

router = APIRouter(tags=["datasets"])

_DEFAULT_INTERNAL_TOKEN = "wire-internal-dev"


def require_internal(
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> None:
    """内部调用鉴权：与 FD_WIRE_INTERNAL_TOKEN 常量时间比对（抄 demands）。"""
    expected = os.getenv("FD_WIRE_INTERNAL_TOKEN", _DEFAULT_INTERNAL_TOKEN)
    if not x_internal_token or not secrets.compare_digest(x_internal_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="内部端点需要有效的 X-Internal-Token",
        )


# ------------------------------------------------------------- 错误响应构造


def drift_response(message: str, differences: list[dict]) -> JSONResponse:
    """409 漂移：detail 带 ``drift:`` 前缀（契约 §1），differences 机器可读。"""
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": message, "differences": differences},
    )


def quota_response(reason: str) -> JSONResponse:
    """429 配额：detail 带 ``quota:`` 前缀 + 恢复途径。"""
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={"detail": f"quota: {reason}"},
    )


def engine_failure(exc: Exception) -> HTTPException:
    """引擎不可达/报错一律 502 fail-closed（绝不假成功）。"""
    prefix = "engine_unavailable" if isinstance(exc, EngineUnavailable) else "engine"
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"{prefix}: {exc}")


# ------------------------------------------------------------- 档位闸（free-tier）

#: 付费面档位错误码（wire-free-tier-data-gate：连接器建集/派生/批量写等
#: cell 耦合路径对无 cell 客户的统一 403 语义）
PLAN_TIER_REQUIRED = "PLAN_TIER_REQUIRED"


def plan_tier_error(action: str) -> HTTPException:
    """403 档位闸：动作需要 cell 物化层（≥基础档）；附机器可读码与升档说明。"""
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": PLAN_TIER_REQUIRED,
            "message": (
                f"{action} 需要 cell 物化层（基础档及以上套餐）；"
                "免费档数据集存于 api 层轻量存储，不参与引擎物化"
            ),
            "upgrade": "升级到基础档及以上套餐后可用（控制台「套餐与定价」）",
        },
    )


def require_cell_tier(dataset: Dataset, action: str) -> None:
    """api 层数据集（storage_tier=api）请求 cell 耦合路径 → 403 档位闸。

    按数据集登记的 tier 判定（升档不迁移：升档客户的旧 api 集仍走本地
    执行，cell 耦合动作依旧拒——数据不在引擎侧，如实不假执行）。
    """
    if getattr(dataset, "storage_tier", service.TIER_CELL) == service.TIER_API:
        raise plan_tier_error(action)


# ------------------------------------------------------------- 内部工具


def _dataset_or_404(db: Session, dataset_id: str, customer_id: str) -> Dataset:
    dataset = db.get(Dataset, dataset_id)
    if dataset is None or dataset.customer_id != customer_id:
        raise HTTPException(status_code=404, detail=f"数据集 {dataset_id} 不存在")
    return dataset


def _connector_or_404(db: Session, connector_id: str, customer_id: str) -> Connector:
    connector = db.get(Connector, connector_id)
    if connector is None or connector.customer_id != customer_id:
        raise HTTPException(status_code=404, detail=f"连接器 {connector_id} 不存在")
    return connector


def _actor(customer_id: str) -> str:
    return f"customer:{customer_id}"


def _ensure_engine_ok(exc: Exception) -> HTTPException:
    return engine_failure(exc)


# ------------------------------------------------------------- 上传


@router.post(
    "/api/v1/datasets/uploads",
    status_code=status.HTTP_201_CREATED,
    response_model=UploadOut,
)
def upload_dataset_file(
    file: UploadFile = File(...),
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> UploadOut:
    """上传 CSV/Excel：解析全量、列与类型预览、50 万行与存储配额 fail-closed。"""
    settings = get_settings()
    filename = file.filename or "upload.csv"
    incoming = file.file.read()
    if not incoming:
        raise HTTPException(status_code=400, detail="上传文件为空")
    upload_dir = Path(settings.datasets_upload_dir)
    tmp_dir = upload_dir / ".incoming"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"{service.new_uuid()}{Path(filename).suffix.lower()}"
    tmp_path.write_bytes(incoming)
    try:
        try:
            stored, parsed, content_sha256 = uploads.store_upload_file(
                source_path=tmp_path,
                original_name=filename,
                upload_dir=upload_dir,
                max_rows=settings.dataset_max_rows,
            )
        except uploads.UploadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        tmp_path.unlink(missing_ok=True)

    try:
        service.ensure_storage_quota(
            db, customer_id=customer_id, incoming_bytes=parsed.size_bytes
        )
    except service.QuotaExceeded as exc:
        Path(stored).unlink(missing_ok=True)
        return _raise_quota(exc)

    record = Upload(
        customer_id=customer_id,
        filename=filename,
        columns_json=parsed.columns,
        row_count=parsed.row_count,
        size_bytes=parsed.size_bytes,
        stored_path=str(stored),
        # 原件内容哈希（客户端发来的原始字节；Excel 为 .xlsx 原件而非转换
        # 后 CSV）——客户可对原件独立复算（wire-provenance-ledger）
        content_sha256=content_sha256,
    )
    db.add(record)
    db.flush()  # 先取 record.id（upload 事件的 subject 要引用）
    # 上传事件入账（wire-provenance-ledger 任务 4.1）：与 uploads 落行同一
    # 事务——append_event 只 flush 不 commit，事务边界归本路由的 commit；
    # 任何失败回滚时登记行与事件一起消失，不留半条事件。建档时数据集尚
    # 不存在，不查档位恒入账（chain 便宜，档位门只作用于引擎修正类事件）。
    append_event(
        db,
        ledger=customer_id,
        type="dataset.upload.registered",
        actor={"kind": "customer", "id": customer_id, "sig": None},
        subject={"kind": "upload", "id": record.id},
        payload={
            "filename": filename,
            "content_sha256": content_sha256,
            "size_bytes": parsed.size_bytes,
            "row_count": parsed.row_count,
        },
    )
    db.commit()
    db.refresh(record)
    return UploadOut(
        upload_id=record.id,
        filename=record.filename,
        columns=[ColumnIn(**c) for c in parsed.columns],
        row_count=parsed.row_count,
        within_limit=True,
        sample_rows=parsed.sample_rows,
    )


def _raise_quota(exc: service.QuotaExceeded):
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=f"quota: {exc.reason}",
    )


# ------------------------------------------------------------- 建档 / 列表


@router.post(
    "/api/v1/datasets",
    status_code=status.HTTP_201_CREATED,
    response_model=DatasetOut,
)
def create_dataset(
    payload: DatasetCreateIn,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
    engine_factory: EngineClientFactory = Depends(get_engine_client_factory),
) -> DatasetOut:
    """建档：tier 在建集时刻按客户档位判定（免费→api 层、付费→cell 层）。

    upload 类：api 层走本地物化（零引擎调用）；cell 层立即 ingest 引擎。
    connector/declared 类：cell 耦合路径，免费档 403 档位闸。
    """
    source = payload.source
    tier = service.storage_tier_for(customer_id=customer_id)
    if tier == service.TIER_API:
        # 免费档两闸前置：建集闸（个数）在分流前统一强制
        try:
            service.ensure_free_dataset_quota(db, customer_id=customer_id)
        except service.QuotaExceeded as exc:
            _raise_quota(exc)
    else:
        try:
            service.ensure_new_dataset_room(db, customer_id=customer_id)
        except service.QuotaExceeded as exc:
            _raise_quota(exc)
    if source.kind == "upload":
        if tier == service.TIER_API:
            return _create_from_upload_local(
                db, customer_id=customer_id, name=payload.name, source=source
            )
        return _create_from_upload(
            db, customer_id=customer_id, name=payload.name, source=source,
            engine_factory=engine_factory,
        )
    if source.kind == "connector":
        if tier == service.TIER_API:
            raise plan_tier_error("连接器建集")
        return _create_from_connector(
            db, customer_id=customer_id, name=payload.name, source=source
        )
    if source.kind == "declared":
        if tier == service.TIER_API:
            raise plan_tier_error("声明式建集")
        return _create_from_declared(
            db, customer_id=customer_id, name=payload.name, source=source,
            engine_factory=engine_factory,
        )
    raise HTTPException(  # pragma: no cover - pydantic Literal 已拦
        status_code=400, detail="source.kind 必须是 upload、connector 或 declared"
    )


def _create_from_declared(
    db: Session,
    *,
    customer_id: str,
    name: str,
    source,
    engine_factory: EngineClientFactory,
) -> DatasetOut:
    """声明式建集（wire-customer-data-engine 2.3）：业务系统不经过文件/连接器，
    直接声明列/类型/主键建空集，随后经数据面批量写填充。引擎 create_declared
    内部走 ingest 原语，产物与上传/连接器数据集无差别。"""
    if not source.columns:
        raise HTTPException(status_code=400, detail="declared 来源必须带 columns（列声明）")
    columns = [c.model_dump() for c in source.columns]
    pk_columns = list(source.pk_columns or [])
    key = service.allocate_key(db, customer_id=customer_id, name=name)
    client = engine_factory(customer_id)
    try:
        result = client.call(
            TOOL_CREATE_DECLARED,
            {
                "dataset_key": key,
                "columns": columns,
                "pk_columns": pk_columns,
                "name": name,
                "actor": _actor(customer_id),
                "channel": "console",
            },
        )
    except (EngineUnavailable, EngineCallError) as exc:
        raise _ensure_engine_ok(exc) from exc
    if not isinstance(result, dict) or "error" in result:
        raise _ensure_engine_ok(
            EngineCallError(str(result.get("error") if isinstance(result, dict) else result))
        )
    dataset = Dataset(
        customer_id=customer_id,
        key=key,
        name=name,
        source_kind="declared",
        connector_id=None,
        source_ref=None,
        columns_json=columns,
        row_count=0,
        size_bytes=0,
        sync_status="ok",
    )
    db.add(dataset)
    db.commit()
    db.refresh(dataset)
    return DatasetOut.from_dataset(dataset)


def _create_from_connector(
    db: Session, *, customer_id: str, name: str, source
) -> DatasetOut:
    if not source.connector_id:
        raise HTTPException(status_code=400, detail="connector 来源必须带 connector_id")
    if not source.source_name:
        raise HTTPException(
            status_code=400,
            detail="connector 来源必须带 source_name（连接器本地源名）",
        )
    if not source.source_ref:
        raise HTTPException(
            status_code=400, detail="connector 来源必须带 source_ref（源内表名/文件名）"
        )
    connector = _connector_or_404(db, source.connector_id, customer_id)
    if connector.revoked_at is not None:
        raise HTTPException(status_code=409, detail="连接器已吊销，不能绑定数据集")
    dataset = Dataset(
        customer_id=customer_id,
        key=service.allocate_key(db, customer_id=customer_id, name=name),
        name=name,
        source_kind="connector",
        connector_id=connector.id,
        source_name=source.source_name,
        source_ref=source.source_ref,
        columns_json=[],
        row_count=0,
        size_bytes=0,
        sync_cron=source.schedule_cron,
        sync_status="idle",
    )
    db.add(dataset)
    db.commit()
    db.refresh(dataset)
    return DatasetOut.from_dataset(dataset)


def _create_from_upload(
    db: Session,
    *,
    customer_id: str,
    name: str,
    source,
    engine_factory: EngineClientFactory,
) -> DatasetOut:
    if not source.upload_id:
        raise HTTPException(status_code=400, detail="upload 来源必须带 upload_id")
    record = db.get(Upload, source.upload_id)
    if record is None or record.customer_id != customer_id:
        raise HTTPException(status_code=404, detail=f"上传 {source.upload_id} 不存在")
    if record.consumed_at is not None:
        raise HTTPException(status_code=409, detail="该上传已建过数据集（单次消费）")
    columns = [dict(c) for c in (record.columns_json or [])]
    try:
        service.ensure_storage_quota(
            db, customer_id=customer_id, incoming_bytes=record.size_bytes
        )
    except service.QuotaExceeded as exc:
        _raise_quota(exc)

    key = service.allocate_key(db, customer_id=customer_id, name=name)
    client = engine_factory(customer_id)
    actor = _actor(customer_id)
    ingest_id: str | None = None
    committed: dict
    try:
        begun = client.call(
            TOOL_INGEST_BEGIN,
            {
                "dataset_key": key,
                "columns": columns,
                "pk_columns": [],
                "row_count": record.row_count,
                "actor": actor,
            },
        )
        ingest_id = str(begun.get("ingest_id") or "")
        if not ingest_id:
            raise EngineCallError("引擎 ingest_begin 未返回 ingest_id")
        start_index = 0
        for chunk in uploads.iter_ingest_chunks(Path(record.stored_path), columns):
            client.call(
                TOOL_INGEST_CHUNK,
                {"ingest_id": ingest_id, "rows": chunk, "start_index": start_index},
            )
            start_index += len(chunk)
        committed = client.call(TOOL_INGEST_COMMIT, {"ingest_id": ingest_id})
    except (EngineUnavailable, EngineCallError) as exc:
        if ingest_id:
            try:
                client.call(TOOL_INGEST_ABORT, {"ingest_id": ingest_id})
            except Exception:  # noqa: BLE001 - best-effort 清理，不掩盖原因
                pass
        raise _ensure_engine_ok(exc) from exc

    row_count = int(committed.get("rows") or record.row_count)
    size_bytes = int(committed.get("size_bytes") or record.size_bytes)
    dataset = Dataset(
        customer_id=customer_id,
        key=key,
        name=name,
        source_kind="upload",
        connector_id=None,
        source_ref=record.filename,
        columns_json=columns,
        row_count=row_count,
        size_bytes=size_bytes,
        sync_status="ok",
    )
    # 引擎已落库后的最终核对：若精确尺寸越限，保留快照但暂停后续写入。
    limit = get_settings().dataset_storage_limit_bytes
    if service.dataset_storage_total(db, customer_id=customer_id) + size_bytes > limit:
        dataset.sync_status = "paused_quota"
        dataset.paused_reason = (
            f"存储用量超过配额 {limit} 字节（本数据集 {size_bytes} 字节）——"
            "请删除数据集或提高配额"
        )
    record.consumed_at = service.utcnow()
    db.add(dataset)
    db.commit()
    db.refresh(dataset)
    # 质量快照钩子（wire-asset-catalog 4.3）：上传建档数据刚落引擎——首提
    # 即触发一次快照（triggered_by=create）；失败隔离，快照失败绝不影响
    # 建档主流程（结果已在头顶 commit 落定）。api 层建档不挂钩（引擎侧无
    # 其数据表，见 quality.service.notify_dataset_changed）。
    try:
        from ..quality import service as quality_service  # noqa: PLC0415 - 惰性防环

        quality_service.notify_dataset_changed(dataset, "create", client=client)
    except Exception:  # noqa: BLE001 - 双保险：主流程绝不因快照中断
        pass
    return DatasetOut.from_dataset(dataset)


def _create_from_upload_local(
    db: Session,
    *,
    customer_id: str,
    name: str,
    source,
) -> DatasetOut:
    """免费档建档：api 层本地物化（wire-free-tier-data-gate D4）。

    CSV 解析/类型推断/分块读取全部复用 :mod:`.uploads`（同一暂存件、同一
    归一语义），行落 ``dataset_rows``、列落 ``dataset_columns``——全链路
    **零引擎调用**。写入闸（行数上限）先于任何行写入，超限即 429（上传
    件不置 consumed、事务回滚，不留半写状态）。
    """
    if not source.upload_id:
        raise HTTPException(status_code=400, detail="upload 来源必须带 upload_id")
    record = db.get(Upload, source.upload_id)
    if record is None or record.customer_id != customer_id:
        raise HTTPException(status_code=404, detail=f"上传 {source.upload_id} 不存在")
    if record.consumed_at is not None:
        raise HTTPException(status_code=409, detail="该上传已建过数据集（单次消费）")
    columns = [dict(c) for c in (record.columns_json or [])]
    try:
        service.ensure_storage_quota(
            db, customer_id=customer_id, incoming_bytes=record.size_bytes
        )
        service.ensure_free_row_quota(record.row_count)  # 写入闸：先于任何行写入
    except service.QuotaExceeded as exc:
        _raise_quota(exc)

    key = service.allocate_key(db, customer_id=customer_id, name=name)
    dataset = Dataset(
        customer_id=customer_id,
        key=key,
        name=name,
        source_kind="upload",
        storage_tier=service.TIER_API,
        connector_id=None,
        source_ref=record.filename,
        columns_json=columns,
        row_count=0,
        size_bytes=0,
        sync_status="ok",
    )
    db.add(dataset)
    db.flush()  # 先取 dataset.id（行/列登记要引用）
    try:
        api_store.ingest_local(
            db,
            dataset=dataset,
            path=Path(record.stored_path),
            columns=columns,
        )
    except service.QuotaExceeded as exc:
        # 行闸途中兜底：整事务回滚（数据集/列/行一起消失，无半写状态）
        db.rollback()
        _raise_quota(exc)
    record.consumed_at = service.utcnow()
    db.commit()
    db.refresh(dataset)
    return DatasetOut.from_dataset(dataset)


@router.get("/api/v1/datasets", response_model=list[DatasetOut])
def list_datasets(
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> list[DatasetOut]:
    """本企业数据集列表（卡片；api 层与 cell 层同列呈现，tier 徽标区分）。"""
    datasets = (
        db.execute(
            select(Dataset)
            .where(Dataset.customer_id == customer_id)
            .order_by(Dataset.created_at.desc(), Dataset.id)
        )
        .scalars()
        .all()
    )
    return [DatasetOut.from_dataset(d) for d in datasets]


@router.get("/api/v1/datasets/quota", response_model=DatasetQuotaOut)
def datasets_quota(
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> DatasetQuotaOut:
    """配额视图（wire-free-tier-data-gate §7，与 web 预约定）。

    免费档返回真实数字（个数/行数上限来自 env 可覆盖的配置项）；付费档
    max 为 null（无此限额展示）。档位不可解析时按付费口径呈现（max=null，
    不放大免费额度；建集闸仍按 fail-closed 独立强制）。
    """
    settings = get_settings()
    used = service.free_dataset_count(db, customer_id=customer_id)
    if service.is_free_tier(customer_id=customer_id):
        return DatasetQuotaOut(
            plan_code=service.FREE_PLAN_CODE,
            used_datasets=used,
            max_datasets=settings.free_tier_max_datasets,
            max_rows_per_dataset=settings.free_tier_max_rows,
        )
    return DatasetQuotaOut(
        plan_code=service.plan_code_for(customer_id=customer_id) or "",
        used_datasets=used,
        max_datasets=None,
        max_rows_per_dataset=None,
    )


@router.get("/api/v1/datasets/{dataset_id}", response_model=DatasetOut)
def get_dataset(
    dataset_id: str,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> DatasetOut:
    dataset = _dataset_or_404(db, dataset_id, customer_id)
    if getattr(dataset, "storage_tier", service.TIER_CELL) == service.TIER_API:
        # schema 预览复用 dataset_columns 聚合（任务 2.4）：列/类型以列注册表
        # 为准、行数以 dataset_rows 实时计数（卡片快照之外的真值）。
        stats = api_store.schema_of(db, dataset_id=dataset.id)
        out = DatasetOut.from_dataset(dataset)
        out.columns = [ColumnIn(**c) for c in stats["columns"]]
        out.row_count = stats["row_count"]
        return out
    return DatasetOut.from_dataset(dataset)


@router.delete("/api/v1/datasets/{dataset_id}")
def delete_dataset(
    dataset_id: str,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
    engine_factory: EngineClientFactory = Depends(get_engine_client_factory),
) -> dict:
    """删除：cell 层先调引擎 drop 三表与元数据（不可达即 502，绝不假删）；
    api 层删本地行/列存储（零引擎调用）。

    同步历史行**保留不删**（dataset_syncs append-only，触发器层禁删；
    行内只有状态/行数/错误文本，无客户数据——孤儿化由设计接受，
    wire-provenance-ledger「不可变登记基线」）。
    """
    dataset = _dataset_or_404(db, dataset_id, customer_id)
    if getattr(dataset, "storage_tier", service.TIER_CELL) == service.TIER_API:
        api_store.delete_local_rows(db, dataset_id=dataset.id)
        db.delete(dataset)
        db.commit()
        return {"deleted": True}
    client = engine_factory(customer_id)
    try:
        client.call(TOOL_DELETE, {"dataset_key": dataset.key, "actor": _actor(customer_id)})
    except (EngineUnavailable, EngineCallError) as exc:
        raise _ensure_engine_ok(exc) from exc
    db.delete(dataset)
    db.commit()
    return {"deleted": True}


# ------------------------------------------------------------- 预览 / 修正


@router.get("/api/v1/datasets/{dataset_id}/rows")
def preview_rows(
    dataset_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
    engine_factory: EngineClientFactory = Depends(get_engine_client_factory),
) -> dict:
    """行预览：cell 层透传引擎 ``preview``（含修正后的有效视图 +
    ``__row_id``）；api 层本地执行（``__row_id``=建集行序）。"""
    dataset = _dataset_or_404(db, dataset_id, customer_id)
    if getattr(dataset, "storage_tier", service.TIER_CELL) == service.TIER_API:
        result = api_store.preview_rows(
            db, dataset_id=dataset.id, page=page, page_size=page_size
        )
        return {**result, "page": page, "page_size": page_size}
    client = engine_factory(customer_id)
    try:
        result = client.call(
            TOOL_PREVIEW,
            {
                "dataset_key": dataset.key,
                "limit": page_size,
                "offset": (page - 1) * page_size,
                "include_corrected": True,
            },
        )
    except (EngineUnavailable, EngineCallError) as exc:
        raise _ensure_engine_ok(exc) from exc
    return {**result, "page": page, "page_size": page_size}


def _form_inner_for_correction(
    ai_db: Session, artifact_id: int, *, customer_id: str, dataset_id: str
) -> dict:
    """corrections 携 ``artifact_id`` 时的表单归属裁决（design D1）。

    本企业 + kind=form + 当前版本 ``spec.datasetId`` 与目标数据集一致——
    任一不符 422 定位原因（不是 404：携带 artifact_id 是显式表单提交声明，
    校验不符如实说明）。返回当前版本 spec 内层（字段校验+提交闸的依据）。
    """
    # 局部导入防环：artifacts 包 __init__ → routes → query_contract 引用
    # 本模块的 _dataset_or_404（模块级导入会在本模块装载中途回环）
    from ..artifacts import service as artifacts_service
    from ..artifacts.models import Artifact

    artifact = ai_db.get(Artifact, artifact_id)
    if artifact is None or artifact.customer_id != customer_id:
        raise HTTPException(
            status_code=422, detail=f"表单产物 {artifact_id} 不存在"
        )
    if artifact.kind != "form":
        raise HTTPException(
            status_code=422,
            detail=f"产物 {artifact_id} 是 {artifact.kind}，仅 form 产物可作为提交执法依据",
        )
    spec = artifacts_service.current_spec(ai_db, artifact)
    inner = spec.get("spec") if isinstance(spec, dict) else None
    if not isinstance(inner, dict) or not isinstance(inner.get("fields"), list):
        raise HTTPException(
            status_code=422,
            detail=f"表单产物 {artifact_id} 当前版本 spec 非法（无字段定义）",
        )
    if inner.get("datasetId") != dataset_id:
        raise HTTPException(
            status_code=422,
            detail=(
                f"表单产物 {artifact_id} 绑定的数据集与目标数据集 {dataset_id} 不一致"
            ),
        )
    return inner


@router.post(
    "/api/v1/datasets/{dataset_id}/corrections",
    status_code=status.HTTP_201_CREATED,
    response_model=CorrectionOut,
)
def add_correction(
    dataset_id: str,
    payload: CorrectionIn,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
    ai_db: Session = Depends(get_ai_db),
    engine_factory: EngineClientFactory = Depends(get_engine_client_factory),
) -> CorrectionOut:
    """修正三件套：改值/删行/加行，落覆盖层（云端不写回客户源）。

    cell 层透传引擎覆盖层；api 层数据集走本地修正层
    （wire-dataset-form-editing，镜像引擎语义——基行永不改写、悬空如实、
    insert 受免费档行数闸），全链路零引擎调用。

    insert 分支经 :func:`~.service.submit_insert_correction` 共享落层
    （wire-form-builder 2.1：表单运行时/公开提交与 corrections 端点同一
    实现，同闸同审计）；异常转译留在路由层，400/429/502 语义与下沉前
    逐字一致。update/delete 分支不动。

    wire-form-entry-advanced D1：insert 请求携 ``artifact_id`` 时先过
    ``_form_inner_for_correction`` 归属裁决，再按该 form 当前版本经共享
    执法点 ``form_runtime.validate_form_submission`` 执行全量字段校验+
    提交闸（命中 422 带 constraint id+message，零写入；归一后 values 落
    insert）——与公开 submit 同一执法实现。不携带时行为与现状逐字一致。
    """
    dataset = _dataset_or_404(db, dataset_id, customer_id)
    if payload.op == "insert":
        values = payload.values
        if payload.artifact_id is not None:
            # 局部导入防环（同 _form_inner_for_correction 注——query_contract
            # 模块级引用本模块，此处顶层导入会在本模块装载中途回环）
            from ..artifacts import form_runtime

            inner = _form_inner_for_correction(
                ai_db, payload.artifact_id, customer_id=customer_id, dataset_id=dataset.id
            )
            normalized, error = form_runtime.validate_form_submission(
                inner,
                payload.values or {},
                datasets_db=db,
                customer_id=customer_id,
                engine_factory=engine_factory,
            )
            if error is not None:
                raise HTTPException(status_code=422, detail=error)
            values = normalized
        try:
            correction_id = service.submit_insert_correction(
                db,
                dataset=dataset,
                values=values,
                actor=_actor(customer_id),
                engine_factory=engine_factory,
            )
        except api_store.LocalQueryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except service.QuotaExceeded as exc:
            _raise_quota(exc)
        except (EngineUnavailable, EngineCallError) as exc:
            raise _ensure_engine_ok(exc) from exc
        return CorrectionOut(correction_id=correction_id)
    if payload.artifact_id is not None:
        # artifact_id 是表单提交执法声明，仅对 insert 有语义（改值/删行不是
        # 表单提交面）——如实拒，不静默忽略
        raise HTTPException(
            status_code=422,
            detail="artifact_id 仅 insert 提交可携带（表单提交执法面）",
        )
    if getattr(dataset, "storage_tier", service.TIER_CELL) == service.TIER_API:
        return _add_local_correction(db, dataset=dataset, payload=payload, customer_id=customer_id)
    if payload.op == "update":
        if not payload.row_id or not payload.column:
            raise HTTPException(
                status_code=400, detail="update 必须带 row_id 与 column"
            )
        if "value" not in payload.model_fields_set:
            raise HTTPException(status_code=400, detail="update 必须带 value（可为 null）")
        arguments = {
            "dataset_key": dataset.key,
            "op": "update",
            "anchor": payload.row_id,
            "values": {payload.column: payload.value},
            "actor": _actor(customer_id),
        }
    else:  # delete
        if not payload.row_id:
            raise HTTPException(status_code=400, detail="delete 必须带 row_id")
        arguments = {
            "dataset_key": dataset.key,
            "op": "delete",
            "anchor": payload.row_id,
            "values": {},
            "actor": _actor(customer_id),
        }
    client = engine_factory(customer_id)
    try:
        result = client.call(TOOL_CORRECTION_ADD, arguments)
    except (EngineUnavailable, EngineCallError) as exc:
        raise _ensure_engine_ok(exc) from exc
    correction_id = result.get("correction_id")
    if correction_id is None:
        raise engine_failure(EngineCallError("引擎未返回 correction_id"))
    return CorrectionOut(correction_id=int(correction_id))


def _add_local_correction(
    db: Session, *, dataset: Dataset, payload: CorrectionIn, customer_id: str
) -> CorrectionOut:
    """api 层本地修正写入（载荷校验与 cell 分支同款 400；悬空允许落层）。"""
    if payload.op == "insert":
        if not payload.values:
            raise HTTPException(status_code=400, detail="insert 必须带 values（整行值）")
        value: object = payload.values
    elif payload.op == "update":
        if not payload.row_id or not payload.column:
            raise HTTPException(
                status_code=400, detail="update 必须带 row_id 与 column"
            )
        if "value" not in payload.model_fields_set:
            raise HTTPException(status_code=400, detail="update 必须带 value（可为 null）")
        value = payload.value
    else:  # delete
        if not payload.row_id:
            raise HTTPException(status_code=400, detail="delete 必须带 row_id")
        value = None
    try:
        correction_id = api_store.apply_local_correction(
            db,
            dataset=dataset,
            op=payload.op,
            row_anchor=payload.row_id,
            column_name=payload.column,
            value=value,
            actor=_actor(customer_id),
        )
    except api_store.LocalQueryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except service.QuotaExceeded as exc:
        _raise_quota(exc)
    return CorrectionOut(correction_id=correction_id)


@router.get("/api/v1/datasets/{dataset_id}/corrections")
def list_corrections(
    dataset_id: str,
    include_reverted: bool = Query(default=True),
    include_dangling: bool = Query(default=True),
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
    engine_factory: EngineClientFactory = Depends(get_engine_client_factory),
) -> dict:
    """修正历史（含悬空与已回滚；审计留痕，不静默丢弃）。

    cell 层透传引擎；api 层数据集走本地修正层清单（零引擎调用，
    字段形态镜像 web 客户端 Correction 类型）。
    """
    dataset = _dataset_or_404(db, dataset_id, customer_id)
    if getattr(dataset, "storage_tier", service.TIER_CELL) == service.TIER_API:
        return {
            "corrections": api_store.list_local_corrections(
                db,
                dataset.id,
                include_reverted=include_reverted,
                include_dangling=include_dangling,
            )
        }
    client = engine_factory(customer_id)
    try:
        result = client.call(
            TOOL_CORRECTION_LIST,
            {
                "dataset_key": dataset.key,
                "include_reverted": include_reverted,
                "include_dangling": include_dangling,
            },
        )
    except (EngineUnavailable, EngineCallError) as exc:
        raise _ensure_engine_ok(exc) from exc
    return result


@router.post("/api/v1/datasets/{dataset_id}/corrections/{correction_id}/revert")
def revert_correction(
    dataset_id: str,
    correction_id: int,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
    engine_factory: EngineClientFactory = Depends(get_engine_client_factory),
) -> dict:
    """单条回滚：标记 reverted 并停止生效；修正留档。

    cell 层透传引擎（重建有效表）；api 层数据集走本地修正层回滚
    （仅置 reverted_at——基行永不改写；二次回滚 409、不存在 404）。
    """
    dataset = _dataset_or_404(db, dataset_id, customer_id)
    if getattr(dataset, "storage_tier", service.TIER_CELL) == service.TIER_API:
        try:
            api_store.revert_local_correction(
                db, dataset=dataset, correction_id=correction_id
            )
        except api_store.CorrectionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except api_store.CorrectionAlreadyReverted as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"reverted": True}
    client = engine_factory(customer_id)
    try:
        result = client.call(
            TOOL_CORRECTION_REVERT,
            {
                "dataset_key": dataset.key,
                "correction_id": correction_id,
                "actor": _actor(customer_id),
            },
        )
    except (EngineUnavailable, EngineCallError) as exc:
        raise _ensure_engine_ok(exc) from exc
    return {"reverted": bool(result.get("reverted", True))}


# ------------------------------------------------------------- CSV 导出（wire-dataset-export）

#: cell 档导出的引擎分页参数：每页 1000 行 × 最多 10 页 = 总量上限 10,000 行
_EXPORT_PAGE_LIMIT = 1000
_EXPORT_MAX_PAGES = 10


def _csv_field(value: Any) -> str:
    """单值 → RFC4180 字段：空值→空串、数值/bool 原样 str()；含逗号/引号/
    换行（CR/LF）的字段以双引号包裹且内部引号翻倍。"""
    if value is None:
        return ""
    text = str(value)
    if any(ch in text for ch in (",", '"', "\n", "\r")):
        return '"' + text.replace('"', '""') + '"'
    return text


def _rows_to_csv(names: list[str], rows: list[dict]) -> str:
    """列名表头 + 数据行 → CSV 文本（行尾 CRLF，RFC4180）；BOM 由调用方
    前置（只加一次，紧贴表头首字符）。"""
    lines = [",".join(_csv_field(name) for name in names)]
    for row in rows:
        lines.append(",".join(_csv_field(row.get(name)) for name in names))
    return "\r\n".join(lines) + "\r\n"


def _export_cell_rows(
    dataset: Dataset, customer_id: str, engine_factory: EngineClientFactory
) -> tuple[list[str], list[dict], bool]:
    """cell 档导出取数：引擎 query 分页循环（1000 行/页，最多 10 页）。

    返回 (列名, 行列表, 是否触顶)：任一页短页即数据见底（未触顶）；10 页
    全满页即**仍可能有更多行**（引擎不承诺 total 精确，短页探测是唯一可靠
    的见底信号）——由端点加 ``X-Export-Truncated: true`` 如实标注，绝不
    静默丢行、不谎报全量。引擎不可达/信封错误一律 502 fail-closed。
    """
    client = engine_factory(customer_id)
    names: list[str] = []
    rows: list[dict] = []
    for page in range(_EXPORT_MAX_PAGES):
        try:
            result = client.call(
                TOOL_QUERY,
                {
                    "dataset_key": dataset.key,
                    "filters": [],
                    "sort": [],
                    "limit": _EXPORT_PAGE_LIMIT,
                    "offset": page * _EXPORT_PAGE_LIMIT,
                },
            )
        except (EngineUnavailable, EngineCallError) as exc:
            raise _ensure_engine_ok(exc) from exc
        if not isinstance(result, dict) or result.get("error"):
            rejected = result.get("error") if isinstance(result, dict) else result
            raise engine_failure(EngineCallError(f"引擎拒绝: {rejected}"))
        if not names:  # 列序以首页响应列卡为准（后续页同集同序）
            cards = result.get("columns")
            if not isinstance(cards, list):
                raise engine_failure(
                    EngineCallError(f"引擎返回缺少 columns 列卡数组: {result!r}")
                )
            for card in cards:
                if not isinstance(card, dict) or not isinstance(card.get("name"), str):
                    raise engine_failure(EngineCallError(f"引擎返回列卡形态异常: {card!r}"))
            names = [card["name"] for card in cards]
        page_rows = result.get("rows")
        if not isinstance(page_rows, list):
            raise engine_failure(EngineCallError(f"引擎返回缺少 rows 行数组: {result!r}"))
        for row in page_rows:  # 行形态异常 fail-closed（绝不静默丢行）
            if not isinstance(row, dict):
                raise engine_failure(EngineCallError(f"引擎返回行形态异常: {row!r}"))
        rows.extend(page_rows)
        if len(page_rows) < _EXPORT_PAGE_LIMIT:
            return names, rows, False
    return names, rows, True


@router.get("/api/v1/datasets/{dataset_id}/export")
def export_dataset_csv(
    dataset_id: str,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
    engine_factory: EngineClientFactory = Depends(get_engine_client_factory),
) -> Response:
    """导出数据集有效视图 CSV（wire-dataset-export）。

    与预览/查询同有效视图口径（含修正）：api 档（免费档）走本地
    ``_effective_rows`` 全量导出——行数天然在免费档单集上限内，无截断；
    cell 档走引擎 query 分页循环（1000 行/页 × 最多 10 页），**总量上限
    10,000 行**：第 10 页仍满页即可能还有更多行，响应头
    ``X-Export-Truncated: true`` 如实标注（客户端据此提示「已截断至前
    10,000 行」），不静默丢行、不谎报全量。

    CSV 约定：UTF-8 带 BOM（Excel 直开）；表头=列名行；值按 RFC4180 转义
    （含逗号/引号/换行 → 双引号包裹与内部引号翻倍；空值→空串；数值/bool
    原样 str）；行尾 CRLF。``Content-Disposition`` 文件名 = 数据集 key.csv；
    跨客户/不存在一律 404（不泄露存在性）。行数有界（api 档免费上限 /
    cell 档 10k 闸），一次性响应即可，无需流式。
    """
    dataset = _dataset_or_404(db, dataset_id, customer_id)
    truncated = False
    if getattr(dataset, "storage_tier", service.TIER_CELL) == service.TIER_API:
        # 免费档本地全量（含修正的有效视图；``__row_id`` 是预览用合成锚，
        # 不属于数据列，导出按列注册表投影自然剔除）。
        stats = api_store.schema_of(db, dataset_id=dataset.id)
        names = [column["name"] for column in stats["columns"]]
        rows = api_store._effective_rows(db, dataset.id)
    else:
        names, rows, truncated = _export_cell_rows(dataset, customer_id, engine_factory)
    csv_text = "\ufeff" + _rows_to_csv(names, rows)
    headers = {"Content-Disposition": f'attachment; filename="{dataset.key}.csv"'}
    if truncated:
        headers["X-Export-Truncated"] = "true"
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers=headers,
    )


# ------------------------------------------------------------- 同步历史 / 恢复


@router.get("/api/v1/datasets/{dataset_id}/syncs", response_model=list[SyncOut])
def list_syncs(
    dataset_id: str,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> list[SyncOut]:
    dataset = _dataset_or_404(db, dataset_id, customer_id)
    rows = (
        db.execute(
            select(DatasetSync)
            .where(DatasetSync.dataset_id == dataset.id)
            .order_by(DatasetSync.started_at.desc(), DatasetSync.id)
        )
        .scalars()
        .all()
    )
    return [SyncOut.model_validate(row) for row in rows]


@router.post("/api/v1/datasets/{dataset_id}/resume", response_model=DatasetOut)
def resume_dataset(
    dataset_id: str,
    payload: ResumeIn,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> DatasetOut:
    """漂移人工确认：接受新 schema（``accept_columns``）后恢复同步。"""
    dataset = _dataset_or_404(db, dataset_id, customer_id)
    if dataset.sync_status != "paused_drift":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"数据集不在漂移暂停态（当前 {dataset.sync_status}），无需恢复",
        )
    dataset.columns_json = [c.model_dump() for c in payload.accept_columns]
    dataset.paused_reason = None
    dataset.sync_status = "idle"
    db.commit()
    db.refresh(dataset)
    return DatasetOut.from_dataset(dataset)


@router.patch("/api/v1/datasets/{dataset_id}/sync-settings", response_model=DatasetOut)
def patch_sync_settings(
    dataset_id: str,
    payload: SyncSettingsPatchIn,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> DatasetOut:
    """增量同步设置（wire-connector-incremental-sync；仅连接器数据集）。

    - 仅 ``source_kind=connector`` 可配（上传数据集没有调度同步概念，422）；
    - 切到 incremental：水位列必须本次提供或已存，且必须是
      ``columns_json`` 既有列名（422 带原因）——列基线为空（尚未首同步）
      时任何列名都不合格；
    - 切回 snapshot：水位两字段清空（watermark_column/watermark_value
      置 NULL）；
    - 换水位列：旧水位值属于旧列（比较语义失效），一并清空——下次同步
      回到全量替换重建基线；
    - ``reset_watermark=true``：只清水位值（模式与列保留），下次同步按
      全量替换重建并重新捕获水位（删除不传播的诚实口径出口）。
    """
    dataset = _dataset_or_404(db, dataset_id, customer_id)
    if dataset.source_kind != "connector":
        raise HTTPException(
            status_code=422,
            detail=f"仅连接器数据集可配置同步设置（当前 source_kind={dataset.source_kind}）",
        )

    new_mode = payload.sync_mode if payload.sync_mode is not None else dataset.sync_mode
    new_column = dataset.watermark_column
    if payload.watermark_column is not None:
        candidate = payload.watermark_column.strip()
        if not candidate:
            raise HTTPException(status_code=422, detail="水位列不能为空")
        new_column = candidate

    if new_mode == "incremental":
        if not new_column:
            raise HTTPException(
                status_code=422,
                detail="增量模式必须指定水位列（watermark_column 本次提供或已存）",
            )
        known = {
            str(c.get("name")) for c in (dataset.columns_json or []) if c.get("name")
        }
        if new_column not in known:
            raise HTTPException(
                status_code=422,
                detail=f"水位列 {new_column} 不在数据集既有列中"
                f"（候选：{sorted(known) or '无（尚未同步）'}）",
            )
    else:
        # snapshot：水位列只在增量模式有意义；快照态显式提供 → 422（fail-closed）
        if payload.watermark_column is not None:
            raise HTTPException(
                status_code=422,
                detail="快照模式没有水位列概念——请先切换 sync_mode=incremental",
            )

    dataset.sync_mode = new_mode
    if new_mode == "snapshot":
        dataset.watermark_column = None
        dataset.watermark_value = None
    elif new_column != dataset.watermark_column:
        dataset.watermark_column = new_column
        dataset.watermark_value = None  # 换列即换坐标系：旧值失效，重建基线
    if payload.reset_watermark:
        dataset.watermark_value = None  # 只清水位值（模式与列保留）
    db.commit()
    db.refresh(dataset)
    return DatasetOut.from_dataset(dataset)


# ------------------------------------------------------------- 连接器管理


@router.post(
    "/api/v1/connectors/pairing-tokens",
    status_code=status.HTTP_201_CREATED,
    response_model=PairingTokenOut,
)
def create_pairing_token(
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> PairingTokenOut:
    """签发配对令牌：15 分钟、单次；库内只存 sha256。"""
    token, token_hash, expires_at = service.new_pairing_token()
    db.add(
        ConnectorPairingToken(
            customer_id=customer_id, token_hash=token_hash, expires_at=expires_at
        )
    )
    db.commit()
    return PairingTokenOut(token=token, expires_at=expires_at)


@router.get("/api/v1/connectors", response_model=list[ConnectorOut])
def list_connectors(
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> list[ConnectorOut]:
    """连接器列表：状态由心跳推导（online/offline/revoked）。"""
    connectors = (
        db.execute(
            select(Connector)
            .where(Connector.customer_id == customer_id)
            .order_by(Connector.created_at.desc(), Connector.id)
        )
        .scalars()
        .all()
    )
    counts = dict(
        db.execute(
            select(Dataset.connector_id, func.count(Dataset.id))
            .where(Dataset.customer_id == customer_id)
            .group_by(Dataset.connector_id)
        ).all()
    )
    return [
        ConnectorOut(
            id=c.id,
            name=c.name,
            version=c.version,
            status=service.connector_status(c),
            last_heartbeat_at=c.last_heartbeat_at,
            created_at=c.created_at,
            revoked_at=c.revoked_at,
            dataset_count=int(counts.get(c.id, 0)),
        )
        for c in connectors
    ]


@router.patch("/api/v1/connectors/{connector_id}", response_model=ConnectorOut)
def rename_connector(
    connector_id: str,
    payload: ConnectorPatchIn,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> ConnectorOut:
    connector = _connector_or_404(db, connector_id, customer_id)
    connector.name = payload.name
    db.commit()
    db.refresh(connector)
    return ConnectorOut(
        id=connector.id,
        name=connector.name,
        version=connector.version,
        status=service.connector_status(connector),
        last_heartbeat_at=connector.last_heartbeat_at,
        created_at=connector.created_at,
        revoked_at=connector.revoked_at,
    )


@router.delete("/api/v1/connectors/{connector_id}")
def revoke_connector(
    connector_id: str,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> dict:
    """吊销：连接器与其全部 key 即时失效（后续推送 401；动作留档）。"""
    connector = _connector_or_404(db, connector_id, customer_id)
    now = service.utcnow()
    connector.revoked_at = connector.revoked_at or now
    keys = (
        db.execute(select(ConnectorKey).where(ConnectorKey.connector_id == connector.id))
        .scalars()
        .all()
    )
    for key in keys:
        key.revoked_at = key.revoked_at or now
    db.commit()
    return {"revoked": True, "keys_revoked": len(keys)}