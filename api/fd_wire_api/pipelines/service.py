"""变换管线服务（wire-transform-pipelines 任务组 2/3/4；design D1/D2/D5）。

## D5 统一代码路（「无第二条物化路径」红线的落地，勿绕开）

步骤的创建与刷新**全部复用既有原语的最小可调单元**：

- **derive / join / filter / project / aggregate / union / sql 创建**：直调
  ``data_plane.data_routes`` 对应端点函数（非 HTTP 自调）——配额闸
  （``ensure_new_dataset_room``）、``allocate_key``、引擎工具调用、
  ``_register_derived_child`` 子集登记、``_sync_columns_and_size`` 列体量
  回填全在既有实现内，参数形状即对应 ``*In``（spec「参数形状一致」）；
  本服务只覆写 ``name`` 为槽位命名。
- **link 创建**：复用 ``entities/routes._call_link_engine``（引擎
  ``customer_dataset_link`` + 旧镜像工具缺失错误矩阵——「引擎暂不支持」
  如实失败）+ ``_register_derived_child`` + ``_sync_columns_and_size``，
  与实体物化端点同一登记路（``source_kind='derived'``、
  ``source_ref=左集 key``）；配额闸 ``ensure_new_dataset_room`` 同 entities。
- **刷新（槽位已绑定）**：统一走 ``data_plane.data_routes.refresh_derived_dataset``
  （引擎 ``customer_dataset_derive_refresh`` 存档规格重放）。**质量快照钩子
  （quality.notify_dataset_changed, triggered_by='materialize'）经本 change
  收拢到该共享函数内部**——此前钩子只挂在物化调度路
  （``materialization/runner._run_derived_refresh`` 尾部），手动刷新与统一
  刷新调度器（schedules/executor）不带快照；收拢后手动刷新/调度刷新/
  管线步骤刷新/级联刷新四路同源，快照与级联语义不分叉。

级联语义：管线链内的「级联」= 按步骤序逐槽位原地刷新（恰为链式级联的
执行顺序）；步骤输出的**非管线**派生子不随 run 级联（它们各有刷新通道，
幂等重放无害——design 风险面「双调度叠加」的缓解一致）。

## 输出槽位（design D2）

（管线, 版本, 步骤序号）→ 稳定输出数据集：首跑创建即绑定
（``transform_pipeline_slots``），同版本重跑原地刷新不新建，新版本新槽位，
旧版本输出以普通数据集存续。输出命名
``<pipeline-name>.v<version>.<seq>.<step-name>``——中间段固定、两端截断
保总长 ≤ 200（``Dataset.name`` 列宽），可追溯管线/版本/步骤。
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..data_plane.auth import DataPlanePrincipal
from ..datasets import service as datasets_service
from ..datasets.ingest import EngineClientFactory
from ..datasets.models import Dataset
from .models import (
    PipelineRun,
    PipelineRunStep,
    TransformPipeline,
    TransformPipelineSlot,
    TransformPipelineVersion,
)

#: 步骤类型白名单（v1 线性链三原语 + wire-transform-operators 四算子与 SQL）
VALID_STEP_OPS: tuple[str, ...] = (
    "derive",
    "join",
    "link",
    "filter",
    "project",
    "aggregate",
    "union",
    "sql",
)

#: 触发方式（spec：调度触发与手动同等记录并标注）
TRIGGER_MANUAL = "manual"
TRIGGER_SCHEDULE = "schedule"

#: run/step 状态
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
#: step 专属：配额硬停如实定位（spec「配额硬停中途」）；skipped 为预留值
STEP_QUOTA_EXHAUSTED = "quota_exhausted"

#: 运行历史保留量（design 开放问题落定：每管线最近 50 run；裁剪时 step
#: 随 run 一并删除——两级留痕同生共死）
MAX_RUNS_RETAINED = 50

#: 步骤数上限（线性链护栏；与级联深度上限 MAX_CASCADE_DEPTH 同量级）
MAX_STEPS = 10

#: 输出数据集名中间段（固定不截断）：.v<version>.<seq>.
_NAME_MIDDLE = ".v{version}.{seq}."

#: Dataset.name 列宽
_DATASET_NAME_MAX = 200


class InvalidSteps(ValueError):
    """步骤定义不合法（创建/改版时的前置校验错误）。"""


class BaseDatasetNotFound(LookupError):
    """基础数据集不存在或不属于本客户。"""


class PipelineVersionNotFound(LookupError):
    """版本不存在（历史版本可查可跑，编号越界才报）。"""


class StepFailure(RuntimeError):
    """步骤执行失败（服务内业务性失败——输入缺失/槽位悬空等）。"""


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------- 校验与命名


def validate_steps(steps: object) -> list[dict]:
    """步骤列表前置校验：只验 op/name/类型与数量（参数形状透传——深校验
    在运行时由既有端点/引擎做，spec「参数形状一致」）。"""
    if not isinstance(steps, list) or not steps:
        raise InvalidSteps("steps 须为非空列表（至少一个步骤）")
    if len(steps) > MAX_STEPS:
        raise InvalidSteps(f"步骤数超过上限 {MAX_STEPS}（v1 线性链护栏）")
    cleaned: list[dict] = []
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise InvalidSteps(f"第 {index} 个步骤须为对象")
        name = step.get("name")
        if not isinstance(name, str) or not name.strip():
            raise InvalidSteps(f"第 {index} 个步骤缺少 name（非空字符串）")
        op = step.get("op")
        if op not in VALID_STEP_OPS:
            raise InvalidSteps(
                f"第 {index} 个步骤 op 非法（{op!r}）——只支持 {'/'.join(VALID_STEP_OPS)}"
            )
        params = step.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise InvalidSteps(f"第 {index} 个步骤 params 须为对象")
        cleaned.append({"name": name.strip()[:200], "op": op, "params": params})
    return cleaned


def slot_dataset_name(
    pipeline_name: str, version: int, seq: int, step_name: str
) -> str:
    """输出数据集命名：``<pipeline>.v<version>.<seq>.<step-name>``。

    中间段固定（可追溯性所在），两端按预算截断保总长 ≤ Dataset.name 列宽。
    """
    middle = _NAME_MIDDLE.format(version=version, seq=seq)
    budget = _DATASET_NAME_MAX - len(middle)
    half = budget // 2
    pipe = (pipeline_name or "pipeline")[:half].rstrip()
    step = step_name[: budget - half].rstrip()
    return f"{pipe}{middle}{step}"


# ---------------------------------------------------------------- CRUD 与版本


def _require_base_dataset(db: Session, customer_id: str, dataset_id: str) -> Dataset:
    dataset = db.get(Dataset, dataset_id)
    if dataset is None or dataset.customer_id != customer_id:
        raise BaseDatasetNotFound(f"基础数据集 {dataset_id} 不存在或不属于本客户")
    return dataset


def create_pipeline(
    db: Session,
    *,
    customer_id: str,
    name: str,
    description: str,
    base_dataset_id: str,
    steps: object,
    created_by: str,
) -> TransformPipeline:
    """建管线 + 版本 1（append-only 版本链的起点）。"""
    cleaned = validate_steps(steps)
    _require_base_dataset(db, customer_id, base_dataset_id)
    pipeline = TransformPipeline(
        customer_id=customer_id,
        name=name.strip()[:200],
        description=description or "",
        base_dataset_id=base_dataset_id,
        current_version=1,
    )
    db.add(pipeline)
    db.flush()
    db.add(
        TransformPipelineVersion(
            pipeline_id=pipeline.id,
            version=1,
            base_dataset_id=base_dataset_id,
            steps_json=cleaned,
            created_by=(created_by or customer_id)[:64],
        )
    )
    db.commit()
    db.refresh(pipeline)
    return pipeline


def update_pipeline(
    db: Session,
    pipeline: TransformPipeline,
    *,
    name: str | None = None,
    description: str | None = None,
    base_dataset_id: str | None = None,
    steps: object = None,
    actor_sub: str = "",
) -> tuple[TransformPipeline, int | None]:
    """整量更新：仅元信息（名称/描述）→ 就地改不动版本；步骤或基础数据集
    变更 → 追加新版本（append-only，旧版本原样可查）。

    返回 (pipeline, version_created)——version_created 仅在新版本时非 None。
    """
    steps_changed = steps is not None
    base_changed = base_dataset_id is not None and base_dataset_id != pipeline.base_dataset_id
    # steps 提供即视为变更意图（与既有端点整量替换口径一致：同值重提交也
    # 出新版本——append-only 链以提交为准，不做参数等价性 diff，design D2
    # 备选 B 明确否掉 diff 语义）
    if steps_changed:
        cleaned = validate_steps(steps)
    if base_changed:
        _require_base_dataset(db, pipeline.customer_id, base_dataset_id)

    if name is not None:
        pipeline.name = name.strip()[:200]
    if description is not None:
        pipeline.description = description

    version_created: int | None = None
    if steps_changed or base_changed:
        version_created = pipeline.current_version + 1
        db.add(
            TransformPipelineVersion(
                pipeline_id=pipeline.id,
                version=version_created,
                base_dataset_id=base_dataset_id or pipeline.base_dataset_id,
                steps_json=cleaned if steps_changed else _current_steps(db, pipeline),
                created_by=(actor_sub or pipeline.customer_id)[:64],
            )
        )
        pipeline.current_version = version_created
        if base_changed:
            pipeline.base_dataset_id = base_dataset_id
    db.commit()
    db.refresh(pipeline)
    return pipeline, version_created


def _current_steps(db: Session, pipeline: TransformPipeline) -> list[dict]:
    row = _version_row(db, pipeline, pipeline.current_version)
    return list(row.steps_json or [])


def _version_row(
    db: Session, pipeline: TransformPipeline, version: int
) -> TransformPipelineVersion:
    row = db.execute(
        select(TransformPipelineVersion).where(
            TransformPipelineVersion.pipeline_id == pipeline.id,
            TransformPipelineVersion.version == version,
        )
    ).scalar_one_or_none()
    if row is None:
        raise PipelineVersionNotFound(
            f"管线 {pipeline.id} 无版本 {version}（当前版本 {pipeline.current_version}）"
        )
    return row


# ---------------------------------------------------------------- 执行引擎


def _slot(
    db: Session, pipeline_id: str, version: int, seq: int
) -> TransformPipelineSlot | None:
    return db.execute(
        select(TransformPipelineSlot).where(
            TransformPipelineSlot.pipeline_id == pipeline_id,
            TransformPipelineSlot.version == version,
            TransformPipelineSlot.seq == seq,
        )
    ).scalar_one_or_none()


def _bind_slot(
    db: Session, pipeline_id: str, version: int, seq: int, dataset_id: str
) -> None:
    db.add(
        TransformPipelineSlot(
            pipeline_id=pipeline_id, version=version, seq=seq, dataset_id=dataset_id
        )
    )
    db.commit()


def _error_text(exc: Exception) -> str:
    """异常 → 步骤错误摘要（HTTPException.detail 可能是 dict/str）。"""
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            return str(detail.get("message") or detail)
        return str(detail)
    return str(exc)


def _link_key_columns(params: dict, side: str) -> list[str]:
    """link 步骤的连接键列（两套键名都收，返回列表）。

    - ``left_column``/``right_column``：实体物化端点口径（单列名或数组）；
    - ``left_keys``/``right_keys``：控制台编辑器口径（数组）。

    引擎侧两种都接受（``_join_key_columns`` 归一），因此这里归一为列表即可。
    """
    for keys in (f"{side}_column", f"{side}_keys"):
        raw = params.get(keys)
        if raw is None:
            continue
        if isinstance(raw, str):
            cols = [raw.strip()] if raw.strip() else []
        elif isinstance(raw, list):
            cols = [str(v).strip() for v in raw if str(v).strip()]
        else:
            continue
        if cols:
            return cols
    return []


def _create_step_output(
    db: Session,
    *,
    pipeline: TransformPipeline,
    version: int,
    seq: int,
    step: dict,
    input_dataset: Dataset,
    engine_factory: EngineClientFactory,
    principal: DataPlanePrincipal,
) -> Dataset:
    """首跑创建（八分支，D4/D5 统一代码路）。

    - derive / join / filter / project / aggregate / union / sql：复用
      ``data_plane`` 的端点函数（配额闸、allocate_key、引擎调用、父边登记、
      列体量回填全在既有实现内——「无第二条物化路径」）；
    - link：复用 entities 的引擎调用 + 同一登记路（历史原因参数走右集+键列）。

    新算子遇旧 cell 引擎时，端点内部给出 502「引擎暂不支持」的如实失败，
    本函数不额外探测（省一次往返，沿 link 先例）。
    """
    from ..data_plane.data_routes import (
        AggregateDeriveIn,
        DeriveIn,
        FilterIn,
        JoinIn,
        ProjectIn,
        SqlIn,
        UnionIn,
        _register_derived_child,
        _sync_columns_and_size,
        dataset_aggregate_derive,
        dataset_derive,
        dataset_filter,
        dataset_join,
        dataset_project,
        dataset_sql,
        dataset_union,
    )
    from ..datasets.routes import _actor

    op = step["op"]
    params = dict(step.get("params") or {})
    name = slot_dataset_name(pipeline.name, version, seq, step["name"])

    if op == "derive":
        body = DeriveIn(name=name, **params)
        out = dataset_derive(input_dataset.id, body, principal, db, engine_factory)
        return db.get(Dataset, out.id)
    if op == "join":
        body = JoinIn(name=name, **params)
        out = dataset_join(input_dataset.id, body, principal, db, engine_factory)
        return db.get(Dataset, out.id)
    if op == "filter":
        body = FilterIn(name=name, **params)
        out = dataset_filter(input_dataset.id, body, principal, db, engine_factory)
        return db.get(Dataset, out.id)
    if op == "project":
        body = ProjectIn(name=name, **params)
        out = dataset_project(input_dataset.id, body, principal, db, engine_factory)
        return db.get(Dataset, out.id)
    if op == "aggregate":
        body = AggregateDeriveIn(name=name, **params)
        out = dataset_aggregate_derive(input_dataset.id, body, principal, db, engine_factory)
        return db.get(Dataset, out.id)
    if op == "union":
        body = UnionIn(name=name, **params)
        out = dataset_union(input_dataset.id, body, principal, db, engine_factory)
        return db.get(Dataset, out.id)
    if op == "sql":
        body = SqlIn(name=name, **params)
        out = dataset_sql(input_dataset.id, body, principal, db, engine_factory)
        return db.get(Dataset, out.id)

    # link（参数形状对齐 entities/routes.py 物化端点的引擎调用契约：
    # 右集 + 左/右连接列；左集 = 本步骤输入数据集）
    #
    # 连接列两套键都收：``left_column``/``right_column``（实体物化端点口径，
    # 单列或复合数组）与 ``left_keys``/``right_keys``（控制台编辑器口径，
    # 数组）。**两套都要**——只认一套会让另一处建的 link 步骤跑到执行时
    # 才报「参数须含连接键列」，是控制台与引擎之间的一处口径裂缝。
    right_dataset = db.get(Dataset, str(params.get("right_dataset_id") or ""))
    if right_dataset is None or right_dataset.customer_id != pipeline.customer_id:
        raise StepFailure(
            f"link 右数据集不存在或不属于本客户: {params.get('right_dataset_id')!r}"
        )
    left_column = _link_key_columns(params, "left")
    right_column = _link_key_columns(params, "right")
    if not left_column or not right_column:
        raise StepFailure("link 参数须含左/右连接键列（left_column 或 left_keys）")
    datasets_service.ensure_new_dataset_room(db, customer_id=pipeline.customer_id)
    from ..entities.routes import _call_link_engine

    child_key = datasets_service.allocate_key(
        db, customer_id=pipeline.customer_id, name=name
    )
    result = _call_link_engine(
        engine_factory,
        pipeline.customer_id,
        {
            "child_key": child_key,
            "left_key": input_dataset.key,
            "left_column": left_column,
            "right_key": right_dataset.key,
            "right_column": right_column,
            "actor": _actor(pipeline.customer_id),
            "channel": principal.channel,
        },
    )
    child = _register_derived_child(
        db,
        principal=principal,
        source=input_dataset,
        name=name,
        engine_result={**result, "dataset_key": child_key},
        extra_parents=[right_dataset],
        origin="link",
    )
    _sync_columns_and_size(db, child, engine_factory, principal)
    return child


def run_pipeline(
    db: Session,
    pipeline: TransformPipeline,
    *,
    version: int | None = None,
    trigger: str = TRIGGER_MANUAL,
    engine_factory: EngineClientFactory,
    actor: str | None = None,
) -> tuple[PipelineRun, list[PipelineRunStep]]:
    """按版本线性执行一遍管线（同步推进、逐步留痕）。

    语义（spec「运行历史两级留痕与失败隔离」）：
    - run 行先落 ``running``，逐步追加 step 行；
    - 槽位已绑定 → 统一路刷新（``refresh_derived_dataset``，含质量快照钩子）；
      未绑定 → 经既有原语创建并绑定槽位；
    - 步骤失败（引擎错/配额硬停/输入缺失）→ 该步骤如实记 failed 或
      quota_exhausted，run 记 failed，**剩余步骤无 step 行**（未执行且无
      记录），已成功步骤的输出保留；
    - 全部成功 → run=success；结束裁剪历史至最近 ``MAX_RUNS_RETAINED`` 条。
    """
    from ..data_plane.data_routes import refresh_derived_dataset

    version_no = pipeline.current_version if version is None else int(version)
    version_row = _version_row(db, pipeline, version_no)
    steps = list(version_row.steps_json or [])

    run = PipelineRun(
        pipeline_id=pipeline.id,
        version=version_no,
        trigger=trigger,
        status=STATUS_RUNNING,
        started_at=_utcnow_naive(),
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    principal = DataPlanePrincipal(
        customer_id=pipeline.customer_id,
        channel="scheduler" if trigger == TRIGGER_SCHEDULE else "console",
        actor_id=actor or pipeline.customer_id,
    )

    step_rows: list[PipelineRunStep] = []
    input_id = version_row.base_dataset_id
    run_error: str | None = None

    for seq, step in enumerate(steps, start=1):
        step_name = str(step.get("name") or f"step-{seq}")
        op = str(step.get("op") or "")
        slot = _slot(db, pipeline.id, version_no, seq)
        started = time.monotonic()
        status = STATUS_SUCCESS
        output_id: str | None = None
        row_count: int | None = None
        error: str | None = None
        try:
            if slot is not None:
                # 同版本重跑：原地刷新（统一路，含质量快照/级联语义——见模块
                # docstring D5 段）。槽位悬空（输出数据集被删）如实失败。
                dataset = db.get(Dataset, slot.dataset_id)
                if dataset is None or dataset.customer_id != pipeline.customer_id:
                    raise StepFailure(
                        f"步骤 {seq} 输出数据集已删除（槽位悬空: {slot.dataset_id}）"
                        "——请新建版本重建输出"
                    )
                refresh_derived_dataset(db, dataset, engine_factory, principal)
                output_id = dataset.id
                row_count = dataset.row_count
            else:
                source = db.get(Dataset, input_id)
                if source is None or source.customer_id != pipeline.customer_id:
                    raise StepFailure(f"步骤 {seq} 输入数据集不存在: {input_id}")
                child = _create_step_output(
                    db,
                    pipeline=pipeline,
                    version=version_no,
                    seq=seq,
                    step=step,
                    input_dataset=source,
                    engine_factory=engine_factory,
                    principal=principal,
                )
                _bind_slot(db, pipeline.id, version_no, seq, child.id)
                output_id = child.id
                row_count = child.row_count
        except datasets_service.QuotaExceeded as exc:
            status = STEP_QUOTA_EXHAUSTED
            error = _error_text(exc)
            if slot is not None:
                output_id = slot.dataset_id  # 既有输出保留（引用如实）
        except (HTTPException, StepFailure, ValidationError, ValueError) as exc:
            status = STATUS_FAILED
            error = _error_text(exc)
            if slot is not None:
                output_id = slot.dataset_id
        step_row = PipelineRunStep(
            run_id=run.id,
            seq=seq,
            name=step_name,
            op=op,
            status=status,
            output_dataset_id=output_id,
            row_count=row_count,
            duration_ms=int((time.monotonic() - started) * 1000),
            error_text=error,
        )
        db.add(step_row)
        db.commit()
        step_rows.append(step_row)
        if status != STATUS_SUCCESS:
            run_error = f"步骤 {seq}（{step_name}/{op}）失败: {error}"
            break  # 失败隔离：剩余步骤不执行、不留 step 行
        input_id = output_id

    run.status = STATUS_FAILED if run_error else STATUS_SUCCESS
    run.finished_at = _utcnow_naive()
    run.error_text = run_error
    db.commit()
    _prune_runs(db, pipeline.id)
    db.refresh(run)
    return run, step_rows


def _prune_runs(db: Session, pipeline_id: str, *, keep: int = MAX_RUNS_RETAINED) -> None:
    """历史保留：每管线只留最近 ``keep`` 条 run（更旧的连同 step 一并删——
    两级留痕同生共死；definition 不受影响，槽位/版本永不裁剪）。"""
    stale_ids = list(
        db.execute(
            select(PipelineRun.id)
            .where(PipelineRun.pipeline_id == pipeline_id)
            .order_by(PipelineRun.id.desc())
            .offset(keep)
        ).scalars()
    )
    if not stale_ids:
        return
    db.execute(
        delete(PipelineRunStep).where(PipelineRunStep.run_id.in_(stale_ids))
    )
    db.execute(delete(PipelineRun).where(PipelineRun.id.in_(stale_ids)))
    db.commit()
