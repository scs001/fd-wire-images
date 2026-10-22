"""个人偏好读写端点（wire-personal-preferences）。

- ``GET /api/console/me/preferences``：三键全量（未设=null）。**行不存在
  不 404**——未写过偏好是常态（新 sub/新设备首拉），全 null 即「未设」。
- ``PUT /api/console/me/preferences``：三键任意子集的部分更新——缺键
  不动、显式 null 清空（model_fields_set 区分「未携」与「携 null」）；
  成功响应=写后全量态（同 GET 形，含读时自愈语义）。

身份走 ``require_console_member_identity``（任意已映射身份**含 viewer**，
无方法级门禁——偏好是 per-sub 个人 UI 数据，不是客户业务数据；详见
auth.py 该依赖 docstring）。

双会话纪律（生产分库 wire_platform/wire_datasets）：偏好行在平台库
（admin.db 会话，ConsoleBase）；置顶看板校验查 ai 库的 ``artifacts``
（``ai.db.get_db``，与 artifacts 路由同一依赖对象——测试覆写同点生效）。

置顶校验（写入与 GET 自愈共用 ``_pinned_violation``）：
- 不存在 / **他户同文案** ``置顶看板不存在``（不泄露跨租户存在性）；
- kind≠dashboard → ``置顶对象不是看板``；
- archived → ``置顶看板已归档，不能设为默认``。

读时自愈（spec「置顶指针自愈」）：GET 对存量 pinned 重验，任一失效 →
返回 null 且**同请求清列**（死链不留；清列失败仅忽略，响应仍 null）。
偏好零客户业务数据，不经溯源账本、不进 team_events。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..admin.db import get_db as get_console_db
from ..ai.db import get_db as get_ai_db
from ..artifacts.models import Artifact, ArtifactStatus
from ..billing.models import Customer, utcnow
from . import auth as console_auth
from .models import MemberPreference

router = APIRouter(tags=["console"])

#: ui_locale 值域（应用层校验；DB 不设 CHECK——值域演进免迁移）
UI_LOCALES = frozenset({"zh", "en"})

#: landing_tab 值域 = 控制台十页签（与 web ConsoleView 页签面冻结对表；
#: workbench 为 wire-personal-workbench 新增——漏加则切页签写回被 422
#: 静默吞掉，工作台永远成不了落地候选，生产冒烟实弹抓出）
LANDING_TABS = frozenset(
    {
        "workbench",
        "chat",
        "artifacts",
        "datasets",
        "entities",
        "packs",
        "demands",
        "usage",
        "keys",
        "team",
    }
)


class PreferencesOut(BaseModel):
    """偏好全量态（GET 与 PUT 响应同形；未设=null）。"""

    ui_locale: str | None = None
    landing_tab: str | None = None
    pinned_dashboard_id: int | None = None


class PreferencesUpdateIn(BaseModel):
    """偏好部分更新体：三键任意子集，缺键=不动、显式 null=清空。

    键是否「被携带」以 ``model_fields_set`` 判定（Plain Optional 字段本身
    分不清「未传」与「传 null」），值域校验在路由层（非 Pydantic——
    422 文案是冻结契约，逐字给到 detail）。
    """

    ui_locale: str | None = None
    landing_tab: str | None = None
    pinned_dashboard_id: int | None = None


def _pinned_violation(
    ai_db: Session, customer_name: str, artifact_id: int
) -> str | None:
    """置顶看板校验：合法返回 None，否则返回 422 冻结文案。

    他户与不存在**同文案**（置顶看板不存在）——跨租户探测面不给区分
    信号；kind/归档两项只对本客户产物判定（顺序即优先级：先归属后形态）。
    """
    artifact = ai_db.get(Artifact, artifact_id)
    if artifact is None or artifact.customer_id != customer_name:
        return "置顶看板不存在"
    if artifact.kind != "dashboard":
        return "置顶对象不是看板"
    if artifact.status == ArtifactStatus.archived:
        return "置顶看板已归档，不能设为默认"
    return None


def _read_state(
    db: Session, ai_db: Session, customer_name: str, sub: str
) -> PreferencesOut:
    """读三键全量态（含读时自愈：失效 pinned → null 且清列）。

    清列失败（如库瞬断）仅忽略——响应仍如实 null，下一次 GET 续自愈；
    不因自愈写失败把读面打成 5xx。
    """
    row = db.get(MemberPreference, sub)
    ui_locale = row.ui_locale if row is not None else None
    landing_tab = row.landing_tab if row is not None else None
    pinned = row.pinned_dashboard_id if row is not None else None
    if pinned is not None and _pinned_violation(ai_db, customer_name, pinned):
        pinned = None
        try:
            row.pinned_dashboard_id = None
            row.updated_at = utcnow()
            db.commit()
        except Exception:  # 自愈清列尽力而为：失败不阻塞读响应
            db.rollback()
    return PreferencesOut(
        ui_locale=ui_locale, landing_tab=landing_tab, pinned_dashboard_id=pinned
    )


@router.get("/api/console/me/preferences", response_model=PreferencesOut)
def get_preferences(
    member: tuple[Customer, str] = Depends(console_auth.require_console_member_identity),
    db: Session = Depends(get_console_db),
    ai_db: Session = Depends(get_ai_db),
) -> PreferencesOut:
    """本人偏好三键（未设=null；行不存在不 404；失效置顶读时自愈清列）。"""
    customer, sub = member
    return _read_state(db, ai_db, str(customer.name), sub)


@router.put("/api/console/me/preferences", response_model=PreferencesOut)
def put_preferences(
    payload: PreferencesUpdateIn,
    member: tuple[Customer, str] = Depends(console_auth.require_console_member_identity),
    db: Session = Depends(get_console_db),
    ai_db: Session = Depends(get_ai_db),
) -> PreferencesOut:
    """部分更新：缺键不动、null 清空；校验全过才落库（零半写）。

    响应=写后全量态（同 GET 形——含存量 pinned 的自愈语义，与紧随的
    GET 逐字节一致）。``updated_at`` 显式刷新：同值重写也是一次写。
    """
    customer, sub = member
    customer_name = str(customer.name)
    provided = payload.model_fields_set

    # 校验先行（任一键非法即 422 定位对应键，不动库）
    if (
        "ui_locale" in provided
        and payload.ui_locale is not None
        and payload.ui_locale not in UI_LOCALES
    ):
        raise HTTPException(
            status_code=422,
            detail="ui_locale 仅支持 zh/en",
        )
    if (
        "landing_tab" in provided
        and payload.landing_tab is not None
        and payload.landing_tab not in LANDING_TABS
    ):
        raise HTTPException(
            status_code=422,
            detail="landing_tab 必须是控制台页签之一",
        )
    if "pinned_dashboard_id" in provided and payload.pinned_dashboard_id is not None:
        violation = _pinned_violation(ai_db, customer_name, payload.pinned_dashboard_id)
        if violation is not None:
            raise HTTPException(
                status_code=422, detail=violation
            )

    row = db.get(MemberPreference, sub)
    if row is None:
        row = MemberPreference(sub=sub)
        db.add(row)
    for field in ("ui_locale", "landing_tab", "pinned_dashboard_id"):
        if field in provided:
            setattr(row, field, getattr(payload, field))
    row.updated_at = utcnow()
    db.commit()
    return _read_state(db, ai_db, customer_name, sub)
