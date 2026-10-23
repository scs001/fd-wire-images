"""产物生命周期 API 路由（wire-artifacts-persistence）。

客户侧（Bearer ID token → logto_sub → Customer，与 ai/datasets 切片
同款身份键 ``require_console_customer_id``）：

- ``GET  /api/v1/artifacts``                 本企业产物列表（kind/status 过滤、更新倒序）
- ``GET  /api/v1/artifacts/{id}``             产物详情（他企一律 404）
- ``PUT  /api/v1/artifacts/{id}``             spec 更新（最小校验 → 追加新版本；
                                               kind=form 追加结构校验与
                                               datasetId 属主校验，
                                               wire-form-builder）
- ``GET  /api/v1/artifacts/{id}/versions``     版本历史（append-only，新在前）
- ``GET  /api/v1/artifacts/{id}/versions/{n}`` 查看任一历史版本
- ``POST /api/v1/artifacts/{id}/publish``      发布（draft → published）
- ``POST /api/v1/artifacts/{id}/archive``      归档（→ archived，不删除）
- ``POST /api/v1/artifacts/{id}/restore``      恢复（archived → draft）
- ``POST /api/v1/artifacts/{id}/refresh``      手动刷新（wire-dashboard-refresh：
                                               仅 dashboard；≥1 面板带合法 query
                                               → 契约路径零 LLM 确定性重取
                                               （wire-dashboard-query-contract），
                                               否则 AI 无头轮按面板 source 取数
                                               → 新版本落盘）
- ``POST /api/v1/artifacts/query-preview``      契约试取（wire-panel-query-binding：
                                               面板 query 同形入参 → 同一执行
                                               路径出试取快照，所见即所刷；
                                               不落任何版本）
- ``POST /api/v1/artifacts/{id}/record-view/preview`` record-view 示例键值
                                               试预览（wire-record-view 任务
                                               4.2：console 身份按当前版本
                                               spec、与公开取数端点同一组装
                                               函数取数；不验令牌不生成链接）
- ``GET  /api/v1/artifacts/{id}/draft``        doc 草稿层读取（wire-rich-text-
                                               docs 任务 2.2：编辑器打开优先
                                               草稿层、无则回退当前版本
                                               spec.text——响应 text=null 即
                                               无草稿行）
- ``PUT  /api/v1/artifacts/{id}/draft``        doc 草稿层自动保存（覆盖写、
                                               不产生版本行——design D3；
                                               限长与版本保存同一口径）
- ``POST /api/v1/artifacts``                    空白创建（wire-dashboard-blank-create；
                                               wire-form-builder 扩 kind=form——
                                               白名单 dashboard/form，title
                                               必填非空，v1 = 最小空 spec
                                               （dashboard 空面板 / form 零字段
                                               未绑数据集），无来源会话指针）
- ``POST /api/v1/artifacts/from-draft``        会话草稿导入为 draft 产物

版本行只读——没有任何改写历史版本的端点（append-only 由路由面保证）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..ai.db import get_db
from ..ai.executor import CELL_NOT_PROVISIONED_MESSAGE, CellNotProvisionedError
from ..ai.protocols import LlmClient, ToolExecutor
# LLM/工具执行依赖直接复用 ai 路由的出口函数（同一依赖对象——测试照
# test_ai_api 的 dependency_overrides 手法注入 mock，生产同为网关/cell 实现）。
from ..ai.routes import get_llm_client, get_tool_executor
from ..ai.settings import get_settings
from ..console.auth import require_console_customer_id
from ..datasets import api_store
from ..datasets.db import get_db as get_datasets_db
from ..market.platform_publisher import PLATFORM_PUBLISHER_NAME
from ..datasets.ingest import (
    EngineCallError,
    EngineClientFactory,
    EngineUnavailable,
    get_engine_client_factory,
)
from . import info_contract, query_contract, record_view_runtime, refresh, service
from .models import ArtifactStatus
from .schemas import (
    ArtifactCreateIn,
    ArtifactDetailOut,
    ArtifactFromDraftIn,
    ArtifactOut,
    ArtifactRefreshOut,
    ArtifactUpdateIn,
    ArtifactVersionDetailOut,
    ArtifactVersionOut,
    QueryPreviewOut,
    artifact_detail,
    version_detail,
)

router = APIRouter(tags=["artifacts"])


def _artifact_or_404(db: Session, artifact_id: int, customer_id: str):
    try:
        return service.get_customer_artifact(
            db, artifact_id=artifact_id, customer_id=customer_id
        )
    except service.ArtifactNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/v1/artifacts", response_model=list[ArtifactOut])
def list_artifacts(
    kind: str | None = Query(default=None),
    artifact_status: str | None = Query(default=None, alias="status"),
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> list[ArtifactOut]:
    """本企业产物列表（按更新时间倒序；kind/status 可选过滤）。"""
    rows = service.list_artifacts(
        db, customer_id=customer_id, kind=kind, status=artifact_status
    )
    return [ArtifactOut.model_validate(a) for a in rows]


@router.get("/api/v1/artifacts/{artifact_id}", response_model=ArtifactDetailOut)
def get_artifact(
    artifact_id: int,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> ArtifactDetailOut:
    """产物详情（当前 spec + 状态迁移记录；他企或不存在一律 404）。"""
    artifact = _artifact_or_404(db, artifact_id, customer_id)
    return artifact_detail(artifact, service.current_spec(db, artifact))


@router.get("/api/v1/artifacts/{artifact_id}/evidence")
def get_artifact_evidence(
    artifact_id: int,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> list[dict]:
    """证据引用 + 原文链接（wire-analysis-console 4.7）。

    只读：把当前版本 spec 的 evidence 逐条补上 ``url`` 与 ``quote_ok``，
    供前端“点回原文并高亮”。fd-info 未配置时仍返回结构（url=null），
    不把缺服务伪装成“没有证据”。
    """
    from .evidence import EvidenceError, parse_evidence

    artifact = _artifact_or_404(db, artifact_id, customer_id)
    spec = service.current_spec(db, artifact)
    try:
        items = parse_evidence(spec.get("evidence"))
    except EvidenceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    client = info_contract.client_from_env()
    out: list[dict] = []
    for index, item in enumerate(items):
        url = None
        quote_ok = None
        error = None
        if client is not None:
            try:
                row = client.get_item(item.stable_id)
                url = row.get("url")
                doc_id = row.get("doc_id")
                if doc_id:
                    located = client.locate(doc_id, item.start, item.end)
                    quote_ok = located.get("quote") == item.quote
                    if not quote_ok:
                        error = "quote 与原文片段不一致"
                else:
                    error = "条目缺少 doc_id"
            except info_contract.InfoUnavailable as exc:
                error = str(exc)
        else:
            error = "fd-info 未配置（FD_INFO_URL）"
        out.append(
            {
                "index": index,
                "claim": item.claim,
                "source": item.source,
                "stable_id": item.stable_id,
                "as_of": item.as_of,
                "start": item.start,
                "end": item.end,
                "quote": item.quote,
                "url": url,
                "quote_ok": quote_ok,
                "error": error,
            }
        )
    return out


@router.put("/api/v1/artifacts/{artifact_id}", response_model=ArtifactDetailOut)
def update_artifact(
    artifact_id: int,
    payload: ArtifactUpdateIn,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
    datasets_db: Session = Depends(get_datasets_db),
) -> ArtifactDetailOut:
    """spec 更新：校验（kind 分派）通过后追加新版本并递增计数（archived
    拒改 409）。

    form 产物的 datasetId 属主校验查 datasets 表 → 必须用 datasets 库会话
    （生产 wire_platform/wire_datasets 分库，ai 会话落不到表；与 refresh/
    query-preview 同款注入）。datasetId 他企/不存在 → 422 零半写。
    """
    artifact = _artifact_or_404(db, artifact_id, customer_id)
    try:
        artifact = service.update_spec(
            db,
            artifact,
            spec=payload.spec,
            customer_id=customer_id,
            datasets_db=datasets_db,
            source_ref=payload.source_ref,
        )
    except service.ArtifactLocked as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except service.InvalidAlertArtifactSpec as exc:
        # alert_rule 规则契约层（wire-alert-evaluation）：400 detail=校验文案
        # （须先于父类 InvalidArtifactSpec 捕获——子类）。
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except service.InvalidArtifactSpec as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return artifact_detail(artifact, service.current_spec(db, artifact))


@router.get(
    "/api/v1/artifacts/{artifact_id}/versions",
    response_model=list[ArtifactVersionOut],
)
def list_versions(
    artifact_id: int,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> list[ArtifactVersionOut]:
    """版本历史（新版本在前；历史版本 append-only 不可改）。"""
    artifact = _artifact_or_404(db, artifact_id, customer_id)
    rows = service.list_versions(db, artifact)
    return [ArtifactVersionOut.model_validate(r) for r in rows]


@router.get(
    "/api/v1/artifacts/{artifact_id}/versions/{version}",
    response_model=ArtifactVersionDetailOut,
)
def get_version(
    artifact_id: int,
    version: int,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> ArtifactVersionDetailOut:
    """查看任一历史版本（含该版本 spec 快照）。"""
    artifact = _artifact_or_404(db, artifact_id, customer_id)
    try:
        row = service.get_version(db, artifact, version)
    except service.ArtifactNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return version_detail(row)


def _transition(artifact_id: int, action: str, customer_id: str, db: Session):
    """状态迁移端点共用骨架：取产物 → 迁移 → 404/409 语义映射。"""
    artifact = _artifact_or_404(db, artifact_id, customer_id)
    handler = {
        "publish": service.publish_artifact,
        "archive": service.archive_artifact,
        "restore": service.restore_artifact,
    }[action]
    try:
        artifact = handler(db, artifact, customer_id=customer_id)
    except service.InvalidStatusTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except service.InvalidArtifactSpec as exc:
        # 发布证据门 enforce 模式（wire-info-stream-program 4.3）：校验失败
        # 422 并指名证据项；archive/restore 不抛此异常，语义不受影响。
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return artifact_detail(artifact, service.current_spec(db, artifact))


@router.post("/api/v1/artifacts/{artifact_id}/publish", response_model=ArtifactDetailOut)
def publish_artifact(
    artifact_id: int,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> ArtifactDetailOut:
    """发布：draft → published（非法迁移 409 说明合法路径）。"""
    return _transition(artifact_id, "publish", customer_id, db)


@router.post("/api/v1/artifacts/{artifact_id}/archive", response_model=ArtifactDetailOut)
def archive_artifact(
    artifact_id: int,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> ArtifactDetailOut:
    """归档：draft/published → archived（不删除，留操作记录）。"""
    return _transition(artifact_id, "archive", customer_id, db)


@router.post("/api/v1/artifacts/{artifact_id}/restore", response_model=ArtifactDetailOut)
def restore_artifact(
    artifact_id: int,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> ArtifactDetailOut:
    """恢复：archived → draft（唯一回退边；恢复后方可再编辑/发布）。"""
    return _transition(artifact_id, "restore", customer_id, db)


@router.post(
    "/api/v1/artifacts",
    status_code=status.HTTP_201_CREATED,
    response_model=ArtifactDetailOut,
)
def create_artifact(
    payload: ArtifactCreateIn,
    response: Response,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
    datasets_db: Session = Depends(get_datasets_db),
) -> ArtifactDetailOut:
    """空白/带内容创建：免费档第一块板的出生路径 + 程序化发布通道。

    产物此前只有 from-draft 一条出生路径（会话草稿转正），而草稿只能由
    AI 构建产生、AI 构建要求 cell——免费档（无 cell）造不出任何看板，
    编辑/契约绑定/刷新等后续能力对第一块板全部够不着。``{kind,title}`` →
    最小空板 spec ``{"title":…, "spec":{"title":…, "panels":[]}}`` 建
    draft 产物 v1，复用既有建档与审计语义（版本行 operator=console:<cid>
    + 初始状态留痕），无来源会话指针；创建即可进编辑器加面板/绑定数据。

    带 ``spec`` 时（add-artifact-create-endpoint）为**程序化发布通道**：
    v1 = 给定 spec（与 spec 更新同一校验栈），**仅限平台自营发布方**
    ``finddata-official``（红线，用户 2026-10-22 裁决「创建权必须限平台
    租户」；与公开橱窗查询层同一判定口径），其他租户 403 如实。幂等键 =
    ``spec.spec.showcase_slug``（import_showcase 既有约定升格为平台契约）：
    同租户命中即不建重——内容一致零操作、有变化追加版本，二者均 200 返回
    既有产物（新建 201 / 重放 200 如实区分）；跨租户同 slug 互不命中。

    语义边界：kind 白名单 v0 仅 dashboard（其他 kind 由 AI 构建产生）
    422；title 缺/空/非字符串 422 如实；校验失败 422 零半写；重放命中
    archived 产物 409（与 PUT 同口径）。
    """
    if payload.spec is not None and customer_id != PLATFORM_PUBLISHER_NAME:
        raise HTTPException(
            status_code=403,
            detail="带内容创建仅限平台自营发布方（finddata-official）；"
            "空白创建（不带 spec）不受限",
        )
    try:
        if payload.spec is None:
            artifact = service.create_blank_artifact(
                db, customer_id=customer_id, kind=payload.kind, title=payload.title,
                source_ref=payload.source_ref,
            )
            created = True
        else:
            artifact, created = service.create_or_replay_artifact(
                db,
                customer_id=customer_id,
                kind=payload.kind,
                title=payload.title,
                spec=payload.spec,
                datasets_db=datasets_db,
            )
    except service.ArtifactLocked as exc:
        # slug 重放命中 archived 产物：拒改与 PUT 同口径（先恢复再重放）
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except service.InvalidAlertArtifactSpec as exc:
        # alert_rule 规则契约层：400 detail=校验文案（须先于父类捕获——子类）
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except service.InvalidArtifactSpec as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not created:
        # slug 重放（零操作或追加版本）：非新建语义，200 如实
        response.status_code = status.HTTP_200_OK
    return artifact_detail(artifact, service.current_spec(db, artifact))


@router.post(
    "/api/v1/artifacts/from-draft",
    status_code=status.HTTP_201_CREATED,
    response_model=ArtifactDetailOut,
)
def import_from_draft(
    payload: ArtifactFromDraftIn,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> ArtifactDetailOut:
    """会话草稿导入：固化为 draft 产物（version=1，保留来源会话指针）。

    会话不存在/非本人、草稿不存在一律 404（不泄露他企存在性）。
    """
    try:
        artifact = service.import_from_draft(
            db,
            customer_id=customer_id,
            conversation_id=payload.conversation_id,
            draft_id=payload.draft_id,
        )
    except service.ArtifactNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except service.InvalidArtifactSpec as exc:  # pragma: no cover - 防御位
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return artifact_detail(artifact, service.current_spec(db, artifact))


def _release(client: object) -> None:
    """释放带 close() 的依赖客户端（生产网关/执行器；测试假件无 close）。"""
    if hasattr(client, "close"):
        client.close()


@router.post(
    "/api/v1/artifacts/{artifact_id}/refresh", response_model=ArtifactRefreshOut
)
def refresh_artifact(
    artifact_id: int,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
    datasets_db: Session = Depends(get_datasets_db),
    llm: LlmClient = Depends(get_llm_client),
    tool_executor: ToolExecutor = Depends(get_tool_executor),
    engine_factory: EngineClientFactory = Depends(get_engine_client_factory),
) -> ArtifactRefreshOut:
    """手动刷新：契约决策——≥1 面板带合法 query → 契约路径（确定性逐面板
    执行，零 LLM，响应 mode=contract + 逐面板结果）；0 个 → AI 无头轮按
    各面板 source 重取数（mode=ai，既有行为零漂移）。两路成功后同 PUT
    语义落新版本（append-only + 审计）。

    语义边界：跨客户 404；kind≠dashboard 403；archived 409（先恢复）；
    同产物刷新进行中再触发 409（进程内单飞锁，两路共用）；AI 路径无
    cell 403 档位提示（零引擎调用）；LLM/引擎失败或契约全面板失败 502
    带原因——**失败零半写**（任何失败都不产生新版本行）。
    """
    artifact = _artifact_or_404(db, artifact_id, customer_id)
    if artifact.kind != "dashboard":
        raise HTTPException(
            status_code=403,
            detail=(
                f"产物 {artifact.id} 是 {artifact.kind}，仅仪表盘（dashboard）"
                "支持数据刷新。"
            ),
        )
    if artifact.status == ArtifactStatus.archived.value:
        raise HTTPException(
            status_code=409,
            detail=(
                f"产物 {artifact.id} 已归档（archived），不可刷新——"
                "请先恢复（restore）为草稿后再刷新。"
            ),
        )
    if not refresh.acquire_refresh_lock(artifact.id):
        raise HTTPException(
            status_code=409,
            detail=(
                f"产物 {artifact.id} 正在刷新中，请稍候再试"
                "（同一产物同时仅允许一次刷新）"
            ),
        )
    try:
        # 契约决策（锁内看当前版本）：≥1 合法 query 面板 → 确定性路径，
        # 不触碰 llm/tool_executor；空计划（旧看板/全非法 query）→ AI 轮。
        current = service.current_spec(db, artifact)
        info_plan = info_contract.info_refresh_plan(current)
        contract_plan = query_contract.contract_refresh_plan(current)
        if info_plan or contract_plan:
            base = current
            panel_results: list[dict] = []
            if info_plan:
                base, info_results = info_contract.refresh_spec_with_info(
                    base,
                    info_contract.client_from_env(),
                    now=service.utc_now_iso(),
                )
                panel_results.extend(r.to_dict() for r in info_results)
            if contract_plan:
                # 契约路径查 datasets 表 → 必须用 datasets 库会话（生产
                # wire_platform/wire_datasets 分库，ai 会话落不到表）。
                base, contract_results = query_contract.refresh_spec_with_contracts(
                    datasets_db,
                    spec=base,
                    customer_id=customer_id,
                    engine_factory=engine_factory,
                )
                panel_results.extend(contract_results)
            new_spec = base
            mode, panels = "contract", panel_results
        else:
            new_spec = refresh.refresh_dashboard_spec(
                current_spec=current,
                llm=llm,
                tool_executor=tool_executor,
                customer_id=customer_id,
                max_turns=get_settings().ai_max_turns,
            )
            mode, panels = "ai", None
        # Q9（wire-artifact-marketplace）：快照时间随版本落盘——每个版本
        # 各自携带刷新时刻，市场渲染面据此披露时效（旧版本无该键 → 前端
        # 诚实显「未知」不猜）。响应与 spec 共用同一时间戳。
        refreshed_at = service.utc_now_iso()
        new_spec["refreshed_at"] = refreshed_at
        # 成功才落盘：同 PUT 语义（契约路径快照即终态、AI 轮内已做最小
        # 校验，此处追加新版本行 + operator 审计 + title 投影）。
        artifact = service.update_spec(db, artifact, spec=new_spec, customer_id=customer_id)
    except CellNotProvisionedError:
        # 无 cell（免费档，仅 AI 路径会走到）：沿用 AI 构建的未开通档位提示。
        raise HTTPException(
            status_code=403, detail=CELL_NOT_PROVISIONED_MESSAGE
        )
    except refresh.RefreshFailed as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except service.InvalidArtifactSpec as exc:  # pragma: no cover - 轮内已校验
        raise HTTPException(status_code=502, detail=f"刷新输出未过校验：{exc}") from exc
    except service.ArtifactLocked as exc:  # pragma: no cover - 竞态归档防御位
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        refresh.release_refresh_lock(artifact.id)
        _release(llm)
        _release(tool_executor)
    return ArtifactRefreshOut(
        version=artifact.version,
        refreshed_at=refreshed_at,
        mode=mode,
        panels=panels,
    )


@router.post("/api/v1/artifacts/query-preview", response_model=QueryPreviewOut)
def query_preview(
    payload: dict,
    customer_id: str = Depends(require_console_customer_id),
    datasets_db: Session = Depends(get_datasets_db),
    engine_factory: EngineClientFactory = Depends(get_engine_client_factory),
) -> QueryPreviewOut:
    """契约试取（wire-panel-query-binding）：入参 = 面板 query 同形
    （dataset_id 必填；filters/sort/limit/columns 可选），经**同一解析**
    （parse_panel_query）与**同一执行路径**（契约刷新的 _query_dataset +
    _build_snapshot——免费档本地零引擎、cell 档引擎 query）返回试取快照
    ——试取与刷新同代码路径，**试取所见即刷新所得**（杜绝两套口径）。

    语义边界：query 非法 422 带原因（试取是人机调试面，必须把非法处如实
    递回，不像刷新路径宽容丢弃）；数据集跨客户/不存在 404（_dataset_or_404
    穿透，不泄露存在性）；执行/快照失败（列不存在、引擎拒绝、免费窄面
    LocalQueryError）400 带原因；试取不读不写产物与版本（保存仍走显式
    保存），故不依赖 ai 库会话——只需 datasets 库会话（档位登记与本地行）。
    """
    query = query_contract.parse_panel_query(payload)
    if query is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "查询契约非法：dataset_id 必须是非空字符串；filters ≤50 项且每项 "
                "column 非空、op ∈ {eq,in,gt,gte,lt,lte,contains,startswith}；"
                "sort ≤10 项且 dir ∈ {asc,desc}；limit 为整数（夹取 1..1000）；"
                "columns 为非空字符串数组（去重保序）"
            ),
        )
    try:
        result = query_contract._query_dataset(
            datasets_db,
            dataset_id=query["dataset_id"],
            customer_id=customer_id,
            query=query,
            engine_factory=engine_factory,
        )
    except api_store.LocalQueryError as exc:  # 免费窄面拒绝（列/算子/值）
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (EngineUnavailable, EngineCallError) as exc:  # cell 未开通/引擎拒绝
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(result, dict) and result.get("error"):
        raise HTTPException(status_code=400, detail=f"引擎拒绝: {result['error']}")
    snapshot, error = query_contract._build_snapshot(query, result)
    if error is not None:  # 形状/投影列不存在等快照失败
        raise HTTPException(status_code=400, detail=error)
    return QueryPreviewOut(snapshot=snapshot, query=query)


class RecordViewPreviewIn(BaseModel):
    """record-view 试预览请求体：``key`` = 示例记录键值（如批次号）。"""

    key: str


@router.post("/api/v1/artifacts/{artifact_id}/record-view/preview")
def record_view_preview(
    artifact_id: int,
    payload: RecordViewPreviewIn,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
    datasets_db: Session = Depends(get_datasets_db),
    engine_factory: EngineClientFactory = Depends(get_engine_client_factory),
) -> dict:
    """示例键值试预览（wire-record-view 任务 4.2 后端侧——console 身份）：

    按**当前版本** spec 经与公开取数端点**同一组装函数**
    （``record_view_runtime.build_record_view_payload``）取数渲染预览——
    试预览所见即扫码所得；不验令牌、不生成公开链接（draft 亦可预览，
    构建器边配边看）。响应形状与公开端点一致（``expires_at`` 恒 null——
    预览无令牌无到期可言）。

    语义边界（如实）：他企/不存在 404；kind≠record-view 422；key 空白 422；
    未绑数据集/键列的中间态 422（先完成配置）；主记录查询失效（keyColumn
    漂移/引擎拒绝）400 带原因（console 是属主调试面——与公开端点的统一
    404 分置）；section 失败单段 error 照常组装。
    """
    artifact = _artifact_or_404(db, artifact_id, customer_id)
    if artifact.kind != service.RECORD_VIEW_KIND:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"产物 {artifact.id} 是 {artifact.kind}，仅单记录页"
                "（record-view）支持试预览。"
            ),
        )
    if not payload.key.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="key 必须是非空字符串（示例记录键值，如批次号）",
        )
    current = service.current_spec(db, artifact)
    envelope = current if isinstance(current, dict) else {}
    inner = envelope.get("spec")
    dataset_id = inner.get("datasetId") if isinstance(inner, dict) else None
    key_column = inner.get("keyColumn") if isinstance(inner, dict) else None
    if (
        not isinstance(dataset_id, str)
        or not dataset_id.strip()
        or not isinstance(key_column, str)
        or not key_column.strip()
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "产物尚未完成主数据集/键列配置（未绑数据集的中间态"
                "不可预览）——请在构建器完成配置后重试。"
            ),
        )
    try:
        body = record_view_runtime.build_record_view_payload(
            inner,
            payload.key,
            datasets_db=datasets_db,
            customer_id=customer_id,
            engine_factory=engine_factory,
        )
    except api_store.LocalQueryError as exc:  # keyColumn 漂移等——如实 400
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (EngineUnavailable, EngineCallError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    title = envelope.get("title")
    if not isinstance(title, str) or not title.strip():
        title = artifact.title
    return {
        "title": title,
        "version": artifact.version,
        "expires_at": None,
        **body,
    }


# --------------------------------------------------------- doc 草稿层端点


# 草稿层请求/响应模型就地定义（schemas.py 归 shared 面，改动面留给本文件；
# 形状只服务 draft 两端口——放 shared 反而扩大耦合面）。
class DocDraftIn(BaseModel):
    """doc 草稿层写入体：``text`` = Markdown 源文本（限长/类型在 service 层
    统一裁决——与版本保存同一校验器，两路口径不分叉）。"""

    text: str


class DocDraftOut(BaseModel):
    """doc 草稿层读写响应：无草稿行时 text/updated_at 均 null（编辑器据此
    回退当前版本 spec.text——design D3 读路径）。"""

    text: str | None
    updated_at: str | None


def _draft_out(row) -> DocDraftOut:
    """草稿行 → 响应（None=无草稿层）。updated_at 用 ISO-8601（同
    ``service.utc_now_iso`` 不留痕口径的字符串形态——响应面不塞 datetime
    对象，前端免时区解析歧义）。"""
    if row is None:
        return DocDraftOut(text=None, updated_at=None)
    return DocDraftOut(
        text=row.text if isinstance(row.text, str) else None,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
    )


@router.get("/api/v1/artifacts/{artifact_id}/draft", response_model=DocDraftOut)
def get_doc_draft(
    artifact_id: int,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> DocDraftOut:
    """读 doc 草稿层（wire-rich-text-docs 任务 2.2 读路径）：编辑器打开时
    优先展示草稿层（未落版的正文不丢）；无草稿行返回 text=null，由前端
    回退当前版本 spec.text（本端点不替前端做合并——「优先草稿、回退版本」
    的判定在前端一处）。

    语义边界：他企/不存在 404（_artifact_or_404 不泄露存在性）；kind≠doc
    422（草稿层只有正文型产物用得上）。draft 状态亦可读（草稿层本为
    draft 而生）；archived 只读不拦读——归档文档仍可看内容。
    """
    artifact = _artifact_or_404(db, artifact_id, customer_id)
    if artifact.kind != service.DOC_KIND:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"产物 {artifact.id} 是 {artifact.kind}，仅文档（doc）"
                "有草稿层。"
            ),
        )
    return _draft_out(service.get_doc_draft(db, artifact))


@router.put("/api/v1/artifacts/{artifact_id}/draft", response_model=DocDraftOut)
def put_doc_draft(
    artifact_id: int,
    payload: DocDraftIn,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> DocDraftOut:
    """自动保存 doc 草稿层（wire-rich-text-docs 任务 2.1/2.2，design D3）：
    **覆盖写**一行，绝不产生版本行——版本链是里程碑（显式「保存版本」走
    ``PUT /api/v1/artifacts/{id}`` 追加），草稿层是工作区（高频自动保存）。

    限长与类型校验复用版本保存同一校验器（doc_spec.validate_doc_text）：
    超限/非字符串 422 载明上限，**零半写**（草稿行不落不半更）。

    语义边界：他企/不存在 404；kind≠doc 422；oversize 422；archived 409
    （与 update_artifact 同映射——先恢复再写）；响应回显落库正文与时刻
    （前端「已保存」态的依据）。
    """
    artifact = _artifact_or_404(db, artifact_id, customer_id)
    try:
        row = service.save_doc_draft(
            db, artifact=artifact, text=payload.text, customer_id=customer_id
        )
    except service.ArtifactLocked as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except service.InvalidArtifactSpec as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _draft_out(row)
