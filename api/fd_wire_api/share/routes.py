"""签名分享链接 API 路由（wire-dashboard-share；form 扩展 wire-form-builder；
公开选项解析端点 wire-form-entry-advanced design D3）。

- ``POST /api/v1/artifacts/{id}/share``（Bearer ID token → Customer，
  与 artifacts 路由同款身份键 ``require_console_customer_id``）：
  仅 published 且 kind∈{dashboard, form} 的产物可分享（否则 403 明确说明）；
  url 按 kind 分派路径（dashboard → ``/api/v1/share/dashboard/…``、
  form → ``/api/v1/share/form/…``）；跨客户一律 404（不泄露他企存在性）。
- ``GET  /api/v1/share/dashboard/{artifact_id}?t=…&v=…``（公开无鉴权）：
  验签 + v 与令牌内版本一致 + 产物仍 published + 钉死版本行在场，
  任一不满足一律 404（detail 统一 "not found"，不泄露差异）。
- ``GET  /api/v1/share/form/{artifact_id}?t=…&v=…``（公开无鉴权，同形）：
  返回钉死版本 spec（仅字段定义与文案，零行数据——公开面只读不读数）。
- ``GET  /api/v1/share/form/{artifact_id}/options?t=…&v=…``（公开受限读面，
  wire-form-entry-advanced design D3）：钉死版本声明的动态选项字段之去重
  选项与 fillFrom 回填映射——secret（503）→ 令牌/published+kind=form/钉死
  版本行（统一 404）→ 令牌级限流（429，独立 env
  ``FD_WIRE_SHARE_OPTIONS_RATE_PER_HOUR`` 缺省 120/h）→ 60 秒服务端短缓存
  （键=产物/版本/令牌 mac——公开页反复刷新不放大查询）→ 200。
- ``POST /api/v1/share/form/{artifact_id}/submit?t=…&v=…``（公开写-only 面，
  design D3 校验序）：secret（503）→ 令牌/published+kind=form/钉死版本行
  （统一 404）→ 按钉死版本 spec 校验 values（422 字段级——经共享执法点
  ``form_runtime.validate_form_submission``：字段校验 → 令牌级限流（429 +
  Retry-After）→ constraints 提交闸（命中 422 带 constraint id+message），
  422 字段级先于 429 的冻结序不变）→ datasets 库会话取数据集（统一 404）
  → 经 ``datasets.service.submit_insert_correction`` 落 insert 修正（actor=
  ``share-form:{artifact_id}``，与 corrections 端点同闸同审计）→ 201。
- ``GET  /api/v1/share/doc/{artifact_id}?t=…&v=…``（公开无鉴权，同形）：
  返回钉死版本 spec（Markdown 正文源文本原样——渲染在 web 侧，零 HTML，
  见 wire-rich-text-docs D1/D2；noindex 亦由分享页负责）。
- ``GET  /api/v1/share/record-view/{artifact_id}?t=…&k=…``（公开实时取数，
  wire-record-view 任务 3.1-3.3）：secret（503）→ 令牌验签+钉死版本+
  published+kind=record-view（统一 404；v 可缺席——URL 规范形态只带 t&k，
  版本一致性以令牌内明文段参与 mac 校验）→ k 缺失/空白 422 如实 → 令牌级
  滑动窗限流（独立 env ``FD_WIRE_SHARE_RECORDVIEW_RATE_PER_HOUR`` 缺省
  240/h；超限 429 + Retry-After）→ 60 秒短缓存（键=产物/钉死版本/令牌
  mac/k——命中零查询）→ 经 ``record_view_runtime.build_record_view_payload``
  组装（与 console preview 同一出处）：主记录 keyColumn 等值查（零行
  200+found:false 不泄露存在性；命中则 header 值 + 各 section 声明查询
  升序/limit/截断/单段 error 如实标注）。仅执行钉死版本 spec 声明的查询，
  绝不开放任意查询透传。

两端点共用一把锁：``FD_WIRE_SHARE_SECRET`` 未配置 → 503 detail 带原因
（逐请求惰性读 env，照 downloads 先例）。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..ai.db import get_db
from ..artifacts import form_runtime, record_view_runtime
from ..artifacts import service as artifacts_service
from ..artifacts.models import Artifact, ArtifactVersion
from ..console.auth import require_console_customer_id
from ..datasets import service as datasets_service
from ..datasets.api_store import LocalQueryError
from ..datasets.db import get_db as get_datasets_db
from ..datasets.ingest import (
    EngineCallError,
    EngineClientFactory,
    EngineUnavailable,
    get_engine_client_factory,
)
from ..datasets.models import Dataset
from ..datasets.routes import engine_failure
from . import rate_limit, service

router = APIRouter(tags=["share"])

#: 公开端点统一失败口径：不带原因差异（未认证方不借此探测链接状态）。
_NOT_FOUND_DETAIL = "not found"

#: 可分享 kind → 公开链接路径段（design D4：url 按 kind 分派；record-view
#: 见 wire-record-view 任务 2.1；doc 见 wire-rich-text-docs D5——正文快照
#: 与看板同款静态读，不开放任何数据面）
SHARE_KIND_PATHS: dict[str, str] = {
    "dashboard": "dashboard",
    "form": "form",
    "record-view": "record-view",
    "doc": "doc",
}


#: 有效期枚举（wire-share-ttl-options；wire-record-view D4 追加 0 档长效
#: 令牌——印刷物/二维码场景）：天数四选一，缺省 30；0=永不过期（exp 置 0，
#: 响应 expires_at=null）
ALLOWED_TTL_DAYS: tuple[int, ...] = (0, 7, 30, 90)


class ShareCreateIn(BaseModel):
    """生成请求体（可选）：``ttl_days`` 有效期四选一（0|7|30|90，缺省 30；
    0=长效令牌——印刷物场景，expires_at 如实 null）。"""

    ttl_days: int | None = None


class ShareLinkOut(BaseModel):
    """生成结果：相对 url（web 拼前缀即用）+ 到期时间 + 钉死的版本号。

    ``expires_at`` 为 0 档长效令牌时为 null（无到期可言——wire-record-view
    design D4 如实呈现）。
    """

    url: str
    expires_at: str | None
    version: int


class SharedDashboardOut(BaseModel):
    """公开打开结果：钉死版本的 spec 快照（信封原样：{title, spec:{…}}）。

    无 iat 侧字段可算 shared_at——省略之，以 expires_at 供前端展示到期
    （0 档长效令牌为 null——wire-record-view D4，无到期可言）。
    """

    title: str
    spec: Any
    version: int
    expires_at: str | None


class SharedDocOut(BaseModel):
    """公开打开文档结果：与 SharedDashboardOut 同形（前端同款消费）。

    doc 的 spec 内是 Markdown 源文本（``{title, spec:{format, text}}``，
    wire-rich-text-docs D1）——原样回传，渲染与 noindex 在 web 分享页；
    服务端零 HTML 透传面（D2 安全底线不在后端重建）。
    """

    title: str
    spec: Any
    version: int
    expires_at: str | None


class SharedFormOut(BaseModel):
    """公开打开表单结果：与 SharedDashboardOut 同形（前端同款消费）。

    form 的 spec 内只有字段定义与文案（零行数据、零读路径——公开面
    write-only 的读侧仅限所有者主动发布的表单结构）。
    """

    title: str
    spec: Any
    version: int
    expires_at: str | None


class FormSubmitIn(BaseModel):
    """公开提交请求体：``values`` = 整行值（按钉死版本 spec 校验）。"""

    values: dict


class FormSubmitOut(BaseModel):
    """公开提交结果：``ok`` 恒 true（失败走 4xx/5xx），``message`` =
    钉死版本 successMessage（缺席回退缺省文案）。"""

    ok: bool = True
    message: str

#: 提交成功缺省文案（spec 未配 successMessage 时）
DEFAULT_SUCCESS_MESSAGE = "提交成功"


def _share_disabled(exc: service.ShareDisabled) -> HTTPException:
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))


@router.post(
    "/api/v1/artifacts/{artifact_id}/share",
    response_model=ShareLinkOut,
)
def create_share_link(
    artifact_id: int,
    payload: ShareCreateIn | None = None,
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> ShareLinkOut:
    """生成分享链接：钉死当前版本，有效期可选 7/30/90 天（缺省 30——
    FD_WIRE_SHARE_TTL_SECONDS 可覆盖缺省；显式 ttl_days 时以选择为准）。"""
    try:
        secret = service.require_share_secret()
    except service.ShareDisabled as exc:
        raise _share_disabled(exc) from exc
    # ttl_days 三态：缺省（无 body / 无字段）走 service 默认；显式给出则
    # 须在枚举内，否则 422 列出合法值（wire-share-ttl-options 1.1）
    if payload is None or payload.ttl_days is None:
        ttl = service.ttl_seconds()
    elif payload.ttl_days not in ALLOWED_TTL_DAYS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"ttl_days 仅支持 {'、'.join(str(d) for d in ALLOWED_TTL_DAYS)}"
                f"（天）——收到 {payload.ttl_days}"
            ),
        )
    else:
        ttl = payload.ttl_days * 24 * 3600
    try:
        artifact = artifacts_service.get_customer_artifact(
            db, artifact_id=artifact_id, customer_id=customer_id
        )
    except artifacts_service.ArtifactNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    # kind 门（design D4）：dashboard 只读渲染、form 只读渲染+公开提交、
    # record-view 公开实时取数（wire-record-view 任务 2.1）、doc 正文快照
    # （wire-rich-text-docs D5）；其余 kind（AI 产物四类等）仍不可分享。
    if artifact.kind not in SHARE_KIND_PATHS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"产物 {artifact.id} 是 {artifact.kind}，仅仪表盘（dashboard）、"
                "表单（form）、单记录页（record-view）与文档（doc）产物支持"
                "生成分享链接。"
            ),
        )
    if artifact.status != "published":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"产物 {artifact.id} 当前状态 {artifact.status}，仅已发布"
                "（published）的产物可分享——请先发布。"
            ),
        )
    # record-view 中间态门（wire-record-view 产物契约：未绑数据集的中间态
    # 可保存、不可生成分享——空键页无取数语义，生成只会得到扫码即 404 的
    # 链接，如实在生成面拦下）
    if artifact.kind == artifacts_service.RECORD_VIEW_KIND:
        spec = artifacts_service.current_spec(db, artifact)
        inner = spec.get("spec") if isinstance(spec, dict) else None
        dataset_id = inner.get("datasetId") if isinstance(inner, dict) else None
        key_column = inner.get("keyColumn") if isinstance(inner, dict) else None
        if (
            not isinstance(dataset_id, str)
            or not dataset_id.strip()
            or not isinstance(key_column, str)
            or not key_column.strip()
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"产物 {artifact.id} 尚未完成主数据集/键列配置"
                    "（未绑数据集的中间态不可生成分享）——请在构建器完成配置"
                    "并保存后重试。"
                ),
            )
    token, exp = service.build_token(
        secret,
        artifact_id=artifact.id,
        version=artifact.version,
        ttl=ttl,
    )
    return ShareLinkOut(
        url=(
            f"/api/v1/share/{SHARE_KIND_PATHS[artifact.kind]}/{artifact.id}"
            f"?t={token}&v={artifact.version}"
        ),
        # 0 档长效令牌 exp=0（无到期）——如实 null，非把 epoch 当到期时间
        expires_at=service.expires_at_iso(exp) if exp > 0 else None,
        version=artifact.version,
    )


@router.get(
    "/api/v1/share/dashboard/{artifact_id}",
    response_model=SharedDashboardOut,
)
def open_shared_dashboard(
    artifact_id: int,
    t: str | None = Query(default=None),
    v: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> SharedDashboardOut:
    """公开打开分享的仪表盘（无鉴权）：返回钉死版本的 spec 快照。

    缺 t/v、验签失败、版本不一致、过期、产物非 published/不存在、版本行
    缺席——一律 404 且 detail 统一（不向未认证方泄露差异）。
    """
    try:
        secret = service.require_share_secret()
    except service.ShareDisabled as exc:
        raise _share_disabled(exc) from exc
    if t is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL)
    try:
        version, exp = service.verify_token(
            secret, artifact_id=artifact_id, token=t, v_param=v
        )
    except service.ShareLinkInvalid:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL)
    artifact = service.get_published_artifact(db, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL)
    row = service.get_pinned_version(db, artifact_id, version)
    if row is None:  # pragma: no cover - 正常链路版本行总在场，防御位
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL)
    # 钉死版本的标题：优先取该版本 spec 信封的 title（快照自洽），
    # 缺席时回退产物当前 title。
    spec = dict(row.spec) if row.spec else {}
    title = spec.get("title")
    if not isinstance(title, str) or not title.strip():
        title = artifact.title
    return SharedDashboardOut(
        title=title,
        spec=spec,
        version=version,
        expires_at=service.expires_at_iso(exp) if exp > 0 else None,
    )


# ----------------------------------------------------- doc 公开面（D5）


@router.get(
    "/api/v1/share/doc/{artifact_id}",
    response_model=SharedDocOut,
)
def open_shared_doc(
    artifact_id: int,
    t: str | None = Query(default=None),
    v: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> SharedDocOut:
    """公开打开分享的文档（无鉴权）：钉死版本的 spec 快照（正文源文本原样）。

    与公开 dashboard 端点同形同闸（缺 t/v、验签失败、版本不一致、过期、
    产物非 published/不存在、版本行缺席——一律 404 统一 detail）。doc 是
    静态快照读面：无取数、无提交，故不设限流与短缓存——限流只保护放大
    底层查询/写入的公开面（record-view/form submit）；noindex 与正文渲染
    属分享页职责（wire-rich-text-docs D2/5.2）。
    """
    try:
        secret = service.require_share_secret()
    except service.ShareDisabled as exc:
        raise _share_disabled(exc) from exc
    if t is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL)
    try:
        version, exp = service.verify_token(
            secret, artifact_id=artifact_id, token=t, v_param=v
        )
    except service.ShareLinkInvalid:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL)
    artifact = service.get_published_artifact(db, artifact_id)
    # kind 复核：dashboard 令牌走不进 doc 路径，反之亦然（与 form 公开面
    # 同款分置——令牌不携带 kind，路径是唯一分派面）
    if artifact is None or artifact.kind != "doc":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL)
    row = service.get_pinned_version(db, artifact_id, version)
    if row is None:  # pragma: no cover - 正常链路版本行总在场，防御位
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL)
    # 钉死版本的标题：优先取该版本 spec 信封的 title（快照自洽），
    # 缺席时回退产物当前 title。
    spec = dict(row.spec) if row.spec else {}
    title = spec.get("title")
    if not isinstance(title, str) or not title.strip():
        title = artifact.title
    return SharedDocOut(
        title=title,
        spec=spec,
        version=version,
        expires_at=service.expires_at_iso(exp) if exp > 0 else None,
    )


# ----------------------------------------------------- form 公开面（D3/D4）


def _not_found() -> HTTPException:
    """公开面统一 404（detail 恒 ``not found``——不泄露失败原因差异）。"""
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL)


def _load_shared_form(
    artifact_id: int, t: str | None, v: int | None, db: Session
) -> tuple[Artifact, ArtifactVersion, int, int]:
    """公开 form 端点共用的前置链（design D3 校验序前四步）。

    secret 在位（否则 503）→ 令牌验签 + v 一致 + 未过期（统一 404）→
    产物 published **且 kind=form**（统一 404——dashboard 令牌走不进 form
    路径，反之亦然）→ 钉死版本行在场（统一 404）。

    返回 ``(artifact, pinned_version_row, version, exp)``。
    """
    try:
        secret = service.require_share_secret()
    except service.ShareDisabled as exc:
        raise _share_disabled(exc) from exc
    if t is None:
        raise _not_found()
    try:
        version, exp = service.verify_token(
            secret, artifact_id=artifact_id, token=t, v_param=v
        )
    except service.ShareLinkInvalid:
        raise _not_found()
    artifact = service.get_published_artifact(db, artifact_id)
    if artifact is None or artifact.kind != "form":
        raise _not_found()
    row = service.get_pinned_version(db, artifact_id, version)
    if row is None:  # pragma: no cover - 正常链路版本行总在场，防御位
        raise _not_found()
    return artifact, row, version, exp


@router.get(
    "/api/v1/share/form/{artifact_id}",
    response_model=SharedFormOut,
)
def open_shared_form(
    artifact_id: int,
    t: str | None = Query(default=None),
    v: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> SharedFormOut:
    """公开打开分享的表单（无鉴权）：钉死版本 spec 快照（零行数据）。

    与公开 dashboard 端点同形同闸（缺 t/v、验签失败、版本不一致、过期、
    产物非 published/非 form/不存在、版本行缺席——一律 404 统一 detail）。
    """
    artifact, row, version, exp = _load_shared_form(artifact_id, t, v, db)
    spec = dict(row.spec) if row.spec else {}
    title = spec.get("title")
    if not isinstance(title, str) or not title.strip():
        title = artifact.title
    return SharedFormOut(
        title=title,
        spec=spec,
        version=version,
        expires_at=service.expires_at_iso(exp) if exp > 0 else None,
    )


@router.post(
    "/api/v1/share/form/{artifact_id}/submit",
    status_code=status.HTTP_201_CREATED,
    response_model=FormSubmitOut,
)
def submit_shared_form(
    artifact_id: int,
    payload: FormSubmitIn,
    t: str | None = Query(default=None),
    v: int | None = Query(default=None),
    db: Session = Depends(get_db),
    datasets_db: Session = Depends(get_datasets_db),
    engine_factory: EngineClientFactory = Depends(get_engine_client_factory),
) -> FormSubmitOut:
    """公开提交一条记录（design D3 校验序，write-only 零读回）：

    1. secret 在位（503）；
    2. 令牌验签 + v 一致 + 未过期（统一 404）；
    3. 产物 published + kind=form（统一 404）；
    4. 钉死版本行在场（统一 404）；
    5. 按钉死版本 spec 经共享执法点 ``form_runtime.validate_form_submission``
       校验（wire-form-entry-advanced）：动态选项实时解析 + 字段校验（422
       字段级——spec.spec 无 fields/非 form 形状 = 404 统一口径）→ 令牌级
       滑动窗限流（429 + Retry-After；超限不落库——冻结序「422 字段级先于
       429」不变）→ constraints 提交闸（命中 422 带 constraint id+message，
       数据集零写入；探测错误按域异常如实转译不静默放行）；
    6. datasets 库会话取数据集（datasetId null/不存在/他企 = 404 统一）；
    7. ``submit_insert_correction``（actor=``share-form:{artifact_id}``——
       修正历史/WAL 可归因到具体表单，与 corrections 端点同闸同审计）。

    异常转译照 corrections 范式：QuotaExceeded→429（quota 原因如实）、
    LocalQueryError→400（列漂移如实）、引擎错→502；全链零半写。
    """

    def _submit_rate_gate() -> None:
        """共享执法点的限流段（stage_gate）：限流键 = 令牌 mac 段（验签已
        保证三段格式；不存完整 token），超限 429 + Retry-After、不落库。"""
        allowed, retry_after = rate_limit.allow(rate_limit.mac_segment(t or ""))
        if not allowed:
            limit = rate_limit.rate_per_hour()
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"提交过于频繁：本链接限每 {int(rate_limit.WINDOW_SECONDS // 60)} 分钟 "
                    f"{limit} 次提交，请 {retry_after} 秒后重试。"
                ),
                headers={"Retry-After": str(retry_after)},
            )

    artifact, row, _version, _exp = _load_shared_form(artifact_id, t, v, db)
    envelope = row.spec if isinstance(row.spec, dict) else {}
    inner = envelope.get("spec")
    fields = inner.get("fields") if isinstance(inner, dict) else None
    if not isinstance(inner, dict) or not isinstance(fields, list):
        # 钉死版本非 form 形状（无字段定义可用）——统一口径，不泄露差异
        raise _not_found()
    normalized, error = form_runtime.validate_form_submission(
        inner,
        payload.values,
        datasets_db=datasets_db,
        customer_id=artifact.customer_id,
        engine_factory=engine_factory,
        stage_gate=_submit_rate_gate,
    )
    if error is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=error
        )
    dataset_id = inner.get("datasetId")
    if not isinstance(dataset_id, str) or not dataset_id.strip():
        # 构建中间态（未绑数据集）不可提交——统一 404
        raise _not_found()
    dataset = datasets_db.get(Dataset, dataset_id)
    if dataset is None or dataset.customer_id != artifact.customer_id:
        raise _not_found()
    try:
        datasets_service.submit_insert_correction(
            datasets_db,
            dataset=dataset,
            values=normalized,
            actor=f"share-form:{artifact_id}",
            engine_factory=engine_factory,
        )
    except LocalQueryError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except datasets_service.QuotaExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"quota: {exc.reason}",
        ) from exc
    except (EngineUnavailable, EngineCallError) as exc:
        raise engine_failure(exc) from exc
    message = inner.get("successMessage")
    if not isinstance(message, str) or not message.strip():
        message = DEFAULT_SUCCESS_MESSAGE
    return FormSubmitOut(ok=True, message=message)


# ------------------------------------- 公开选项解析端点（wire-form-entry-advanced D3）

#: 选项解析响应的服务端短缓存 TTL（秒）：同产物/同版本/同令牌 mac 的
#: 反复刷新不放大底层查询（design D3）
_OPTIONS_CACHE_TTL_SECONDS = 60.0

#: 缓存条目上限（写前机会性清扫过期项——公开令牌量级的有界保护）
_OPTIONS_CACHE_MAX_ENTRIES = 512

_options_cache: dict[tuple[int, int, str], tuple[float, dict]] = {}
_options_cache_lock = threading.Lock()


def _cached_options_payload(cache_key: tuple[int, int, str], build: Callable[[], dict]) -> dict:
    """60 秒短缓存读穿：TTL 内命中直接回放，未命中构建后回填（构建在锁外
    执行——查询慢不阻塞其他令牌的缓存读）。"""
    now = time.monotonic()
    with _options_cache_lock:
        hit = _options_cache.get(cache_key)
        if hit is not None and now - hit[0] < _OPTIONS_CACHE_TTL_SECONDS:
            return hit[1]
    payload = build()
    with _options_cache_lock:
        if len(_options_cache) >= _OPTIONS_CACHE_MAX_ENTRIES:
            for stale in [
                key
                for key, entry in _options_cache.items()
                if now - entry[0] >= _OPTIONS_CACHE_TTL_SECONDS
            ]:
                _options_cache.pop(stale, None)
        _options_cache[cache_key] = (now, payload)
    return payload


def _reset_options_cache() -> None:
    """测试复位入口：清空选项端点短缓存（用例间隔离）。"""
    with _options_cache_lock:
        _options_cache.clear()


@router.get("/api/v1/share/form/{artifact_id}/options")
def shared_form_options(
    artifact_id: int,
    t: str | None = Query(default=None),
    v: int | None = Query(default=None),
    db: Session = Depends(get_db),
    datasets_db: Session = Depends(get_datasets_db),
    engine_factory: EngineClientFactory = Depends(get_engine_client_factory),
) -> dict:
    """公开选项解析端点（design D3——公开面唯一新增读面，受限引用读）：

    1. secret 在位（503）；令牌验签 + v 一致 + 未过期、产物 published +
       kind=form、钉死版本行在场（统一 404——与公开 GET/submit 同链
       ``_load_shared_form``）；钉死版本非 form 形状（无 fields）统一 404；
    2. 令牌级滑动窗限流（独立阈值 ``FD_WIRE_SHARE_OPTIONS_RATE_PER_HOUR``
       缺省 120/h；超限 429 + Retry-After）；
    3. 60 秒短缓存（键=产物/版本/令牌 mac——命中零底层查询）；
    4. 载荷仅含钉死版本声明了 ``optionsFromDataset`` 的字段：
       ``{fields: {column: {options[], truncated, fills, (error)}}}``
       ——去重选项（升序、截断标注）与 fillFrom 回填映射（每选项值×声明
       回填列，D6）；引用失效如实 error 标注（空集不静默）；未声明动态
       选项的表单返回空 fields（非 404）。不返回任何其他行/列。
    """
    artifact, row, version, _exp = _load_shared_form(artifact_id, t, v, db)
    envelope = row.spec if isinstance(row.spec, dict) else {}
    inner = envelope.get("spec")
    fields = inner.get("fields") if isinstance(inner, dict) else None
    if not isinstance(inner, dict) or not isinstance(fields, list):
        raise _not_found()
    allowed, retry_after = rate_limit.allow_options(rate_limit.mac_segment(t or ""))
    if not allowed:
        limit = rate_limit.options_rate_per_hour()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"获取选项过于频繁：本链接限每 {int(rate_limit.WINDOW_SECONDS // 60)} 分钟 "
                f"{limit} 次，请 {retry_after} 秒后重试。"
            ),
            headers={"Retry-After": str(retry_after)},
        )
    cache_key = (artifact_id, version, rate_limit.mac_segment(t or ""))
    return _cached_options_payload(
        cache_key,
        lambda: form_runtime.build_options_payload(
            inner,
            datasets_db=datasets_db,
            customer_id=artifact.customer_id,
            engine_factory=engine_factory,
        ),
    )


# ------------------------------- 公开取数端点（wire-record-view 任务 3.1-3.3）

#: record-view 取数响应的服务端短缓存 TTL（秒）与条目上限：键含 k——印刷物
#: 场景同一批次扫码高频，缓存挡底层查询（design D3；机器同选项端点短缓存）
_RECORDVIEW_CACHE_TTL_SECONDS = 60.0
_RECORDVIEW_CACHE_MAX_ENTRIES = 512

_recordview_cache: dict[tuple[int, int, str, str], tuple[float, dict]] = {}
_recordview_cache_lock = threading.Lock()


def _cached_recordview_payload(
    cache_key: tuple[int, int, str, str], build: Callable[[], dict]
) -> dict:
    """60 秒短缓存读穿（键=产物/钉死版本/令牌 mac/k）：TTL 内命中直接回放
    （零底层查询），未命中构建后回填（构建在锁外——查询慢不阻塞其他键的
    缓存读）。"""
    now = time.monotonic()
    with _recordview_cache_lock:
        hit = _recordview_cache.get(cache_key)
        if hit is not None and now - hit[0] < _RECORDVIEW_CACHE_TTL_SECONDS:
            return hit[1]
    payload = build()
    with _recordview_cache_lock:
        if len(_recordview_cache) >= _RECORDVIEW_CACHE_MAX_ENTRIES:
            for stale in [
                key
                for key, entry in _recordview_cache.items()
                if now - entry[0] >= _RECORDVIEW_CACHE_TTL_SECONDS
            ]:
                _recordview_cache.pop(stale, None)
        _recordview_cache[cache_key] = (now, payload)
    return payload


def _reset_recordview_cache() -> None:
    """测试复位入口：清空 record-view 取数短缓存（用例间隔离）。"""
    with _recordview_cache_lock:
        _recordview_cache.clear()


def _token_version_hint(token: str) -> int | None:
    """v 参数缺席时的版本提示：取令牌明文首段（mac 已覆盖 version——伪造
    即验签失败；这只是给 verify_token 的 v 一致性检查一个可比值，令牌机器
    零变化）。record-view 的 URL 规范形态只带 ``t`` 与 ``k``（design D2/D5），
    但生成端点给出的链接带 ``v``——两种形态都收。"""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        return int(parts[0])
    except ValueError:
        return None


@router.get("/api/v1/share/record-view/{artifact_id}")
def open_shared_record_view(
    artifact_id: int,
    t: str | None = Query(default=None),
    v: int | None = Query(default=None),
    k: str | None = Query(default=None),
    db: Session = Depends(get_db),
    datasets_db: Session = Depends(get_datasets_db),
    engine_factory: EngineClientFactory = Depends(get_engine_client_factory),
) -> dict:
    """公开实时取数（wire-record-view design D2——平台首个公开实时数据读面）：

    1. secret 在位（503）；
    2. 令牌验签 + v 一致（v 缺席按令牌明文段自证）+ 未过期（0 档跳过）+
       产物 published + kind=record-view + 钉死版本行在场（一律统一 404，
       detail 恒 ``not found``——与既有公开端点同形）；
    3. k 缺失/空白 422 如实（键值参数是人机面，不瞒报）；
    4. 令牌级滑动窗限流（独立 env ``FD_WIRE_SHARE_RECORDVIEW_RATE_PER_HOUR``
       缺省 240/h；超限 429 + Retry-After，不落查询）；
    5. 60 秒短缓存（键=产物/钉死版本/令牌 mac/k）；
    6. ``record_view_runtime.build_record_view_payload``（与 console preview
       同一组装函数）：主记录 keyColumn 等值查（零行 → 200 + found:false，
       无 header/sections 键，不泄露记录存在性）；命中 → header 字段值 +
       各 section 声明查询（``{fromField}`` 代入、dateColumn 升序、≤limit、
       截断如实；单段失败=该段 error + 空 rows，其余照常）。

    读边界红线：仅执行钉死版本 spec 声明的查询——不开放任意查询/任意列/
    任意数据集透传；同一令牌可查任意 k（k 不进令牌，枚举边界由限流兜底）。
    主记录查询失效（keyColumn 漂移/LocalQueryError）统一 404（不向未认证方
    泄露差异——属主经 preview 端点拿到如实原因）；引擎失败 502 fail-closed。
    """
    try:
        secret = service.require_share_secret()
    except service.ShareDisabled as exc:
        raise _share_disabled(exc) from exc
    if t is None:
        raise _not_found()
    try:
        version, exp = service.verify_token(
            secret,
            artifact_id=artifact_id,
            token=t,
            v_param=v if v is not None else _token_version_hint(t),
        )
    except service.ShareLinkInvalid:
        raise _not_found()
    artifact = service.get_published_artifact(db, artifact_id)
    if artifact is None or artifact.kind != "record-view":
        raise _not_found()
    row = service.get_pinned_version(db, artifact_id, version)
    if row is None:  # pragma: no cover - 正常链路版本行总在场，防御位
        raise _not_found()
    envelope = row.spec if isinstance(row.spec, dict) else {}
    inner = envelope.get("spec")
    dataset_id = inner.get("datasetId") if isinstance(inner, dict) else None
    key_column = inner.get("keyColumn") if isinstance(inner, dict) else None
    if (
        not isinstance(dataset_id, str)
        or not dataset_id.strip()
        or not isinstance(key_column, str)
        or not key_column.strip()
    ):
        # 钉死版本非 record-view 完成形状（未绑数据集/键列）——统一口径
        raise _not_found()
    if k is None or not k.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="k 必须是非空字符串（记录键值，如批次号）",
        )
    allowed, retry_after = rate_limit.allow_recordview(rate_limit.mac_segment(t))
    if not allowed:
        limit = rate_limit.recordview_rate_per_hour()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"访问过于频繁：本链接限每 {int(rate_limit.WINDOW_SECONDS // 60)} 分钟 "
                f"{limit} 次，请 {retry_after} 秒后重试。"
            ),
            headers={"Retry-After": str(retry_after)},
        )
    cache_key = (artifact_id, version, rate_limit.mac_segment(t), k)

    def _build() -> dict:
        try:
            payload = record_view_runtime.build_record_view_payload(
                inner,
                k,
                datasets_db=datasets_db,
                customer_id=artifact.customer_id,
                engine_factory=engine_factory,
            )
        except LocalQueryError:
            # 主记录失效（keyColumn 漂移等免费窄面拒）——统一 404，不向
            # 未认证方泄露差异；属主经 preview 端点拿到如实 4xx 原因。
            raise _not_found()
        except (EngineUnavailable, EngineCallError) as exc:
            raise engine_failure(exc) from exc
        title = envelope.get("title")
        if not isinstance(title, str) or not title.strip():
            title = artifact.title
        return {
            "title": title,
            "version": version,
            "expires_at": service.expires_at_iso(exp) if exp > 0 else None,
            **payload,
        }

    return _cached_recordview_payload(cache_key, _build)
