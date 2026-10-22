"""变换管线 API 路由（wire-transform-pipelines 任务组 2/3/4）。

客户侧（Bearer ID token → ``require_console_customer_id``，与 entities/
assets 切片同款身份键；表挂 ``DatasetsBase``——datasets 库会话）：

- ``GET    /api/v1/pipelines``                          管线列表（含调度状态与
  最近一次 run 摘要；viewer 可读）
- ``POST   /api/v1/pipelines``                          建管线（版本 1）→ 201
- ``PUT    /api/v1/pipelines/{id}`                      更新：仅名称/描述就地
  改；步骤/基础数据集变更 → 追加新版本（响应带 ``version_created``）
- ``GET    /api/v1/pipelines/{id}`                      详情 + 版本列表
- ``POST   /api/v1/pipelines/{id}/run``                 立即运行（默认当前
  版本，可指定版本；同步执行返回 run+steps）
- ``GET    /api/v1/pipelines/{id}/runs``                运行历史（时间倒序分页）
- ``GET    /api/v1/pipelines/{id}/runs/{run_id}`        单次运行 + 步骤明细
- ``PUT    /api/v1/pipelines/{id}/schedule``            挂/改 cron 调度
- ``GET    /api/v1/pipelines/{id}/schedule``            调度状态
- ``DELETE /api/v1/pipelines/{id}/schedule``            删调度
- ``POST   /api/v1/pipelines/{id}/schedule/pause|resume`` 暂停/恢复（enabled 门）

门禁（spec「客户隔离与权限」）：写操作（POST/PUT/DELETE）经统一依赖的
viewer 方法门 403（owner/editor 可写）；跨客户一律 404 不泄露；未鉴权 401。
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..console.auth import require_console_customer_id
from ..data_plane.data_routes import ScheduleIn
from ..datasets.db import get_db
from ..datasets.ingest import EngineClientFactory, get_engine_client_factory
from ..datasets.models import Dataset
from . import service
from .models import (
    PipelineRun,
    PipelineRunStep,
    TransformPipeline,
    TransformPipelineVersion,
)

router = APIRouter(prefix="/api/v1/pipelines", tags=["pipelines"])

#: materialized_artifacts 上的管线调度行 kind（runner 按 kind 分派到管线运行）
SCHEDULE_KIND = "transform_pipeline"


# ------------------------------------------------------------------ schemas


class StepIn(BaseModel):
    """步骤定义：参数形状与既有原子操作端点透传一致（深校验在运行时）。"""

    name: str = Field(min_length=1, max_length=200)
    op: str
    params: dict = Field(default_factory=dict)


class PipelineCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    base_dataset_id: str = Field(min_length=1, max_length=36)
    steps: list[StepIn] = Field(min_length=1, max_length=10)


class PipelineUpdateIn(BaseModel):
    """全字段可选：只动提供的；步骤/基础数据集变更 ⇒ 新版本（append-only）。"""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    base_dataset_id: str | None = Field(default=None, min_length=1, max_length=36)
    steps: list[StepIn] | None = Field(default=None, min_length=1, max_length=10)


class RunIn(BaseModel):
    version: int | None = Field(default=None, ge=1)


def _pipeline_or_404(db: Session, pipeline_id: str, customer_id: str) -> TransformPipeline:
    pipeline = db.get(TransformPipeline, pipeline_id)
    if pipeline is None or pipeline.customer_id != customer_id:
        raise HTTPException(status_code=404, detail=f"管线 {pipeline_id} 不存在")
    return pipeline


# ------------------------------------------------------------------ 序列化


def _step_payload(step: PipelineRunStep) -> dict:
    return {
        "id": step.id,
        "run_id": step.run_id,
        "seq": step.seq,
        "name": step.name,
        "op": step.op,
        "status": step.status,
        "output_dataset_id": step.output_dataset_id,
        "row_count": step.row_count,
        "duration_ms": step.duration_ms,
        "error_text": step.error_text,
    }


def _run_payload(run: PipelineRun) -> dict:
    return {
        "id": run.id,
        "pipeline_id": run.pipeline_id,
        "version": run.version,
        "trigger": run.trigger,
        "status": run.status,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "error_text": run.error_text,
    }


def _versions_payload(db: Session, pipeline_id: int | str) -> list[dict]:
    rows = (
        db.execute(
            select(TransformPipelineVersion)
            .where(TransformPipelineVersion.pipeline_id == pipeline_id)
            .order_by(TransformPipelineVersion.version.asc())
        )
        .scalars()
        .all()
    )
    return [
        {
            "version": row.version,
            "steps": list(row.steps_json or []),
            "base_dataset_id": row.base_dataset_id,
            "created_by": row.created_by,
            "created_at": row.created_at,
        }
        for row in rows
    ]


def _base_dataset_name(db: Session, dataset_id: str | None) -> str | None:
    if not dataset_id:
        return None
    return db.execute(
        select(Dataset.name).where(Dataset.id == dataset_id)
    ).scalar_one_or_none()


# ------------------------------------------------------------------ 管线 CRUD


@router.get("")
def list_pipelines(
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> dict:
    """管线列表（名称/版本/调度/最近运行状态——viewer 可读，客户隔离）。"""
    from ..materialization.db import get_session as get_mat_session
    from ..materialization.models import MaterializedArtifact

    pipelines = (
        db.execute(
            select(TransformPipeline)
            .where(TransformPipeline.customer_id == customer_id)
            .order_by(TransformPipeline.created_at.desc(), TransformPipeline.id)
        )
        .scalars()
        .all()
    )
    ids = [p.id for p in pipelines]
    by_id = {p.id: p for p in pipelines}
    # 当前版本行（steps_count 以当前版本的步骤定义为准）
    current_versions: dict[str, TransformPipelineVersion] = {}
    if ids:
        for row in (
            db.execute(
                select(TransformPipelineVersion).where(
                    TransformPipelineVersion.pipeline_id.in_(ids)
                )
            )
            .scalars()
            .all()
        ):
            pipeline = by_id.get(row.pipeline_id)
            if pipeline is not None and row.version == pipeline.current_version:
                current_versions[row.pipeline_id] = row
    # 最近一次 run（按 started_at 倒序取首条）
    last_runs: dict[str, PipelineRun] = {}
    if ids:
        runs = (
            db.execute(
                select(PipelineRun)
                .where(PipelineRun.pipeline_id.in_(ids))
                .order_by(PipelineRun.started_at.desc(), PipelineRun.id.desc())
            )
            .scalars()
            .all()
        )
        for run in runs:
            last_runs.setdefault(run.pipeline_id, run)
    # 调度行（kind=transform_pipeline, ref_key=pipeline_id）
    schedules: dict[str, MaterializedArtifact] = {}
    if ids:
        mat = get_mat_session()
        try:
            for row in (
                mat.execute(
                    select(MaterializedArtifact).where(
                        MaterializedArtifact.kind == SCHEDULE_KIND,
                        MaterializedArtifact.ref_key.in_(ids),
                    )
                )
                .scalars()
                .all()
            ):
                schedules[str(row.ref_key)] = row
        finally:
            mat.close()

    items = []
    for pipeline in pipelines:
        version_row = current_versions.get(pipeline.id)
        sched = schedules.get(pipeline.id)
        last_run = last_runs.get(pipeline.id)
        items.append(
            {
                "id": pipeline.id,
                "name": pipeline.name,
                "description": pipeline.description,
                "current_version": pipeline.current_version,
                "steps_count": len(version_row.steps_json or []) if version_row else 0,
                "base_dataset_id": pipeline.base_dataset_id,
                "base_dataset_name": _base_dataset_name(db, pipeline.base_dataset_id),
                "schedule": (
                    None
                    if sched is None
                    else {
                        "cron": sched.schedule,
                        "enabled": sched.enabled,
                        "last_run_at": sched.last_run_at,
                        "last_status": sched.last_status,
                    }
                ),
                "last_run": (
                    None
                    if last_run is None
                    else {
                        "id": last_run.id,
                        "status": last_run.status,
                        "started_at": last_run.started_at,
                        "finished_at": last_run.finished_at,
                        "trigger": last_run.trigger,
                    }
                ),
                "updated_at": pipeline.updated_at,
            }
        )
    return {"items": items, "total": len(items)}


@router.post("", status_code=201)
def create_pipeline(
    body: PipelineCreateIn,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> dict:
    """建管线（版本 1）；op 只认 derive|join|link，参数形状透传（运行时深校验）。"""
    try:
        pipeline = service.create_pipeline(
            db,
            customer_id=customer_id,
            name=body.name,
            description=body.description,
            base_dataset_id=body.base_dataset_id,
            steps=[s.model_dump() for s in body.steps],
            created_by=customer_id,
        )
    except service.InvalidSteps as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except service.BaseDatasetNotFound as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    version_row = service._version_row(db, pipeline, 1)
    return {
        "id": pipeline.id,
        "name": pipeline.name,
        "description": pipeline.description,
        "base_dataset_id": pipeline.base_dataset_id,
        "current_version": pipeline.current_version,
        "steps": list(version_row.steps_json or []),
        "created_at": pipeline.created_at,
    }


@router.put("/{pipeline_id}")
def update_pipeline(
    pipeline_id: str,
    body: PipelineUpdateIn,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> dict:
    """整量更新：仅名称/描述 → 就地改（响应无 version_created）；步骤/基础
    数据集变更 → 追加新版本（响应带 version_created，旧版本原样可查）。"""
    pipeline = _pipeline_or_404(db, pipeline_id, customer_id)
    try:
        pipeline, version_created = service.update_pipeline(
            db,
            pipeline,
            name=body.name,
            description=body.description,
            base_dataset_id=body.base_dataset_id,
            steps=None if body.steps is None else [s.model_dump() for s in body.steps],
            actor_sub=customer_id,
        )
    except service.InvalidSteps as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except service.BaseDatasetNotFound as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    payload = {
        "id": pipeline.id,
        "name": pipeline.name,
        "description": pipeline.description,
        "base_dataset_id": pipeline.base_dataset_id,
        "current_version": pipeline.current_version,
        "updated_at": pipeline.updated_at,
    }
    if version_created is not None:
        payload["version_created"] = version_created
    return payload


@router.get("/{pipeline_id}")
def get_pipeline(
    pipeline_id: str,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> dict:
    """详情 + 全版本列表（历史版本步骤定义原样可查——append-only）。"""
    pipeline = _pipeline_or_404(db, pipeline_id, customer_id)
    return {
        "id": pipeline.id,
        "name": pipeline.name,
        "description": pipeline.description,
        "base_dataset_id": pipeline.base_dataset_id,
        "base_dataset_name": _base_dataset_name(db, pipeline.base_dataset_id),
        "current_version": pipeline.current_version,
        "created_at": pipeline.created_at,
        "updated_at": pipeline.updated_at,
        "versions": _versions_payload(db, pipeline.id),
    }


# ------------------------------------------------------------------ 运行


@router.post("/{pipeline_id}/run")
def run_pipeline(
    pipeline_id: str,
    body: RunIn = Body(default=RunIn()),
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
    engine_factory: EngineClientFactory = Depends(get_engine_client_factory),
) -> dict:
    """立即运行（同步执行；默认当前版本）：返回完成态 run + 逐步骤留痕。"""
    pipeline = _pipeline_or_404(db, pipeline_id, customer_id)
    try:
        run, steps = service.run_pipeline(
            db,
            pipeline,
            version=body.version,
            trigger=service.TRIGGER_MANUAL,
            engine_factory=engine_factory,
        )
    except service.PipelineVersionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"run": _run_payload(run), "steps": [_step_payload(s) for s in steps]}


@router.get("/{pipeline_id}/runs")
def list_runs(
    pipeline_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> dict:
    """运行历史（时间倒序；run 可再展开到步骤明细——下一端点）。"""
    pipeline = _pipeline_or_404(db, pipeline_id, customer_id)
    total = (
        db.execute(
            select(func.count())
            .select_from(PipelineRun)
            .where(PipelineRun.pipeline_id == pipeline.id)
        ).scalar_one()
    )
    rows = (
        db.execute(
            select(PipelineRun)
            .where(PipelineRun.pipeline_id == pipeline.id)
            .order_by(PipelineRun.started_at.desc(), PipelineRun.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        .scalars()
        .all()
    )
    return {"items": [_run_payload(r) for r in rows], "total": total}


@router.get("/{pipeline_id}/runs/{run_id}")
def get_run(
    pipeline_id: str,
    run_id: int,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> dict:
    """单次运行详情（run + steps；run 不属于该管线/跨客户一律 404）。"""
    pipeline = _pipeline_or_404(db, pipeline_id, customer_id)
    run = db.get(PipelineRun, run_id)
    if run is None or run.pipeline_id != pipeline.id:
        raise HTTPException(status_code=404, detail=f"运行 {run_id} 不存在")
    steps = (
        db.execute(
            select(PipelineRunStep)
            .where(PipelineRunStep.run_id == run.id)
            .order_by(PipelineRunStep.seq.asc())
        )
        .scalars()
        .all()
    )
    return {"run": _run_payload(run), "steps": [_step_payload(s) for s in steps]}


# ------------------------------------------------------------------ 调度


def _schedule_row(ref_key: str, customer_id: str):
    """取本管线的 materialized_artifacts 调度行（kind=transform_pipeline）。"""
    from ..materialization.db import get_session as get_mat_session
    from ..materialization.models import MaterializedArtifact

    mat = get_mat_session()
    try:
        row = mat.execute(
            select(MaterializedArtifact).where(
                MaterializedArtifact.kind == SCHEDULE_KIND,
                MaterializedArtifact.ref_key == ref_key,
            )
        ).scalar_one_or_none()
        return mat, row
    except Exception:
        mat.close()
        raise


def _schedule_payload(pipeline_id: str, row) -> dict:
    if row is None:
        return {
            "pipeline_id": pipeline_id,
            "cron": None,
            "enabled": False,
            "last_run_at": None,
            "last_status": None,
        }
    return {
        "pipeline_id": pipeline_id,
        "cron": row.schedule,
        "enabled": row.enabled,
        "last_run_at": row.last_run_at,
        "last_status": row.last_status,
    }


@router.put("/{pipeline_id}/schedule")
def put_schedule(
    pipeline_id: str,
    body: ScheduleIn,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> dict:
    """挂/改管线 cron 调度（复用 materialized_artifacts 调度机器：5 字段
    cron 校验 + 60s 扫描 + 同分钟去重；到点按当前版本运行）。"""
    from ..materialization.cron import parse_cron
    from ..materialization.models import MaterializedArtifact

    pipeline = _pipeline_or_404(db, pipeline_id, customer_id)
    try:
        parse_cron(body.cron)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    mat, row = _schedule_row(pipeline.id, customer_id)
    try:
        if row is None:
            row = MaterializedArtifact(
                artifact_id=None,
                customer_id=customer_id,
                kind=SCHEDULE_KIND,
                ref_key=pipeline.id,
                spec={"pipeline_id": pipeline.id, "name": pipeline.name},
                schedule=body.cron,
                enabled=True,
            )
            mat.add(row)
        else:
            row.schedule = body.cron
            row.enabled = True
        mat.commit()
        mat.refresh(row)
        return _schedule_payload(pipeline.id, row)
    finally:
        mat.close()


@router.get("/{pipeline_id}/schedule")
def get_schedule(
    pipeline_id: str,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> dict:
    _pipeline_or_404(db, pipeline_id, customer_id)
    mat, row = _schedule_row(pipeline_id, customer_id)
    try:
        return _schedule_payload(pipeline_id, row)
    finally:
        mat.close()


def _set_schedule_enabled(
    pipeline_id: str, customer_id: str, *, enabled: bool, db: Session
) -> dict:
    """暂停/恢复共用段：enabled 门翻转（暂停后到点不触发，手动运行不受影响）。"""
    pipeline = _pipeline_or_404(db, pipeline_id, customer_id)
    mat, row = _schedule_row(pipeline.id, customer_id)
    try:
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"管线 {pipeline_id} 未挂调度（先 PUT /schedule）",
            )
        row.enabled = enabled
        mat.commit()
        mat.refresh(row)
        return _schedule_payload(pipeline.id, row)
    finally:
        mat.close()


@router.post("/{pipeline_id}/schedule/pause")
def pause_schedule(
    pipeline_id: str,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> dict:
    return _set_schedule_enabled(pipeline_id, customer_id, enabled=False, db=db)


@router.post("/{pipeline_id}/schedule/resume")
def resume_schedule(
    pipeline_id: str,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> dict:
    return _set_schedule_enabled(pipeline_id, customer_id, enabled=True, db=db)


@router.delete("/{pipeline_id}/schedule")
def delete_schedule(
    pipeline_id: str,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> dict:
    _pipeline_or_404(db, pipeline_id, customer_id)
    mat, row = _schedule_row(pipeline_id, customer_id)
    try:
        if row is not None:
            mat.delete(row)
            mat.commit()
        return {"pipeline_id": pipeline_id, "removed": True}
    finally:
        mat.close()
