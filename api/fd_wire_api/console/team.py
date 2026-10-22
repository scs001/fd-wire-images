"""团队空间（wire-team-spaces 任务 3 / v2）：多队 + 活跃队指针 + 席位闸。

身份模型：owner = ``billing_customers.logto_sub`` 本人（不落成员表），
``customer_members`` 只存被邀成员（editor|viewer）。v2 起一人可多队
（同一 sub 可在多个客户下有成员行）；当前生效队伍由 ``member_active_team``
指针决定（每 sub 至多一行；解析序 = 指针优先 → owner 回退 → 首队回退，
见 auth.resolve_member_role）。

- ``redeem_invite``：POST /api/console/session 携码兑换入口（服务函数，
  routes.py 调用）。同一事务面：建 membership + 码置 used +
  team_events('member.joined') + 首队落指针（该 sub 无指针行时新队即
  活跃队；已有指针不动）。v2 多队解锁：owner/他队成员可正常入新队。
  拒绝序：码无效（不存在/已用/过期）→ 已是该团队成员（owner 或成员行）
  → 席位满（**码保活**不置 used，错误文案含档位与席位数字）。不建新
  客户/不动 sub2api/不铸 key（成员共享邀请方套餐与配额）。
- ``GET  /api/console/teams``：我的队清单（任意已映射身份，个人面）。
- ``POST /api/console/team/switch``：切换活跃队（个人面 UI 态，非团队
  变更——不进 team_events，design D6）。
- ``GET  /api/console/team``：成员名单（含隐含 owner 行）+ 待用邀请列表
  + 席位用量 seats_used/seats_limit（owner-only）。
- ``POST /api/console/team/invites``：生成一次性邀请码（席位满 409 不
  产码）。
- ``PATCH /api/console/team/members/{sub}``：成员角色变更（owner-only；
  同值幂等不留痕）。
- ``DELETE /api/console/team/invites/{id}``：作废邀请（404 如实）。
- ``DELETE /api/console/team/members/{sub}``：撤销成员（owner 行 409；
  不存在 404；成员自撤允许）+ **被撤者活跃队指针处置**（指针指向本队时：
  有其他成员行重指其中 id 最小者，否则删指针行；自撤同路径覆盖）。

席位口径：``seats_used = 成员行数 + 1``（owner 本身占一座）；上限 =
``billing.plans.seat_limit``（按档位 code，limits_json.seats 覆盖）。
每次团队变更 append-only 留痕（team_events，只 INSERT）。
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..admin.db import get_db as get_console_db
from ..billing.models import Customer, utcnow
from ..billing.plans import seat_limit
from . import auth as console_auth
from .models import CustomerInvite, CustomerMember, MemberActiveTeam, TeamEvent

router = APIRouter(tags=["console-team"])

#: 邀请码形状（契约冻结）：``wsi-`` + secrets 24 hex（token_hex(12)）
INVITE_CODE_PREFIX = "wsi-"
#: 邀请码有效期（契约冻结：24 小时）
INVITE_TTL = timedelta(hours=24)


class InviteRedeemError(Exception):
    """邀请兑换拒绝（原因进 502 failed body；路由层转译）。"""


def _aware(dt: datetime) -> datetime:
    """sqlite 往返丢 tzinfo 的 naive UTC → aware UTC（PG 读回本就 aware）。"""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _record_event(
    db: Session, *, customer_id: int, actor_sub: str, action: str, target: str, detail: dict | None = None
) -> None:
    """append-only 留痕（只 INSERT，调用方随事务面提交）。"""
    db.add(
        TeamEvent(
            customer_id=customer_id,
            actor_sub=actor_sub,
            action=action,
            target=target,
            detail=detail,
        )
    )


def _plan_code(customer: Customer) -> str:
    """客户当前档位 code（plan relationship 缺失的腐化态回空串走缺省限额）。"""
    return customer.plan.code if customer.plan else ""


def _seats_used(db: Session, customer_id: int) -> int:
    """已占席位 = 成员行数 + 1（owner 本身占一座）。"""
    count = db.execute(
        select(func.count()).select_from(CustomerMember).where(CustomerMember.customer_id == customer_id)
    ).scalar_one()
    return int(count) + 1


def _seat_full_detail(db: Session, customer: Customer) -> str | None:
    """席位已满的错误文案（含档位 code、已用/上限、升级指引）；未满返回 None。"""
    used = _seats_used(db, customer.id)
    limit = seat_limit(db, _plan_code(customer))
    if used >= limit:
        return (
            f"套餐席位已满（档位 {_plan_code(customer) or '未知'}："
            f"已用 {used}/{limit} 席），升级套餐以邀更多成员"
        )
    return None


def redeem_invite(db: Session, *, sub: str, code: str) -> Customer:
    """兑换邀请码：sub 加入邀请方客户为成员（role=码的 role；v2 多队解锁）。

    拒绝面（InviteRedeemError → 502 failed 带原因，诚实拒；v2 起拒绝序）：
    - 码不存在 / 已被使用 / 已过期；邀请方客户行缺失（腐化态如实报）；
    - 已是该团队成员（sub = 邀请方 owner 或已有 (customer, sub) 成员行）
      ——owner/他队成员入**新队**不受限，只拒同队重复加入；
    - 席位满：seats_used（成员行数+1）≥ seat_limit(档位) → 拒且**码保活**
      （不置 used，腾出席位或升级后可再兑），文案含档位与席位数字。

    通过后同事务面：建成员行 + 码置 used + team_events('member.joined') +
    首队落指针（无 member_active_team 行时插入 = 新队成为活跃队；已有
    指针不动，切换走 POST /api/console/team/switch）。
    """
    invite = db.execute(
        select(CustomerInvite).where(CustomerInvite.code == code)
    ).scalar_one_or_none()
    if invite is None:
        raise InviteRedeemError("邀请码不存在")
    if invite.used_by_sub is not None or invite.used_at is not None:
        raise InviteRedeemError("邀请码已被使用")
    if _aware(invite.expires_at) <= utcnow():
        raise InviteRedeemError("邀请码已过期")
    customer = db.get(Customer, invite.customer_id)
    if customer is None:
        raise InviteRedeemError("邀请方客户不存在")
    if customer.logto_sub == sub:
        raise InviteRedeemError("已是该团队成员（owner），无需重复加入")
    dup = db.execute(
        select(CustomerMember).where(
            CustomerMember.customer_id == customer.id,
            CustomerMember.logto_sub == sub,
        )
    ).scalar_one_or_none()
    if dup is not None:
        raise InviteRedeemError("已是该团队成员，无需重复加入")
    gate_detail = _seat_full_detail(db, customer)
    if gate_detail is not None:
        # 码保活：不置 used——腾位/升级后同码可再兑。
        raise InviteRedeemError(gate_detail)
    db.add(
        CustomerMember(
            customer_id=customer.id,
            logto_sub=sub,
            role=invite.role,
            invited_by_sub=invite.created_by_sub,
        )
    )
    invite.used_by_sub = sub
    invite.used_at = utcnow()
    _record_event(
        db,
        customer_id=customer.id,
        actor_sub=sub,
        action="member.joined",
        target=sub,
        detail={"role": invite.role, "invite_code": code},
    )
    if db.get(MemberActiveTeam, sub) is None:
        # 首队落指针：此前无任何活跃队 → 新队即活跃队；已有指针不动。
        db.add(MemberActiveTeam(logto_sub=sub, customer_id=customer.id))
    db.commit()
    return customer


# ------------------------------------------------------------------ 端点


class TeamMeOut(BaseModel):
    sub: str
    role: str


class TeamMemberOut(BaseModel):
    logto_sub: str
    role: str
    created_at: datetime
    invited_by_sub: str | None = None


class TeamInviteOut(BaseModel):
    id: int
    code: str
    role: str
    expires_at: datetime
    created_at: datetime


class TeamOut(BaseModel):
    me: TeamMeOut
    members: list[TeamMemberOut]
    invites: list[TeamInviteOut]
    #: 席位用量（v2）：成员行数 + 1（owner 占一座）
    seats_used: int
    #: 席位上限（v2）：seat_limit 按档位（limits_json.seats 覆盖）
    seats_limit: int


class InviteCreateIn(BaseModel):
    role: str = Field(pattern="^(editor|viewer)$")


class MemberRoleIn(BaseModel):
    role: str = Field(pattern="^(editor|viewer)$")


class TeamEntryOut(BaseModel):
    """我的队清单单行（v2 多队）：角色 owner|editor|viewer + 是否活跃队。"""

    customer_id: int
    customer_name: str
    role: str
    is_active: bool


class TeamsOut(BaseModel):
    teams: list[TeamEntryOut]
    active_customer_id: int


class TeamSwitchIn(BaseModel):
    customer_id: int


class TeamSwitchOut(BaseModel):
    active_customer_id: int


class DeletedOut(BaseModel):
    deleted: bool = True


@router.get("/api/console/teams", response_model=TeamsOut)
def list_my_teams(
    identity: tuple[Customer, str] = Depends(console_auth.require_console_member_identity),
    db: Session = Depends(get_console_db),
) -> TeamsOut:
    """我的队清单（v2 多队；任意已映射身份，个人面）。

    组装：自有队（resolve_customer 命中 → role='owner'）+ 全部成员行所在
    队；排序自有队在前、成员队按 customer_id。``is_active``/``active_customer_id``
    对齐解析层（指针优先 → owner 回退 → 首队回退）：member_active_team
    有有效指针（指向本清单内某队）用指针；无指针时 owner 队优先，否则
    首个成员行所在队（腐化行——指向已删客户——跳过，与解析层 401/回退
    行为一致）。
    """
    _customer, sub = identity
    own = console_auth.resolve_customer(db, sub)
    # (customer_id, name, role)，插入序即输出序：自有队在前、成员队按 id
    entries: list[tuple[int, str, str]] = []
    seen: set[int] = set()
    if own is not None:
        entries.append((own.id, own.name, "owner"))
        seen.add(own.id)
    member_rows = (
        db.execute(
            select(CustomerMember)
            .where(CustomerMember.logto_sub == sub)
            .order_by(CustomerMember.id)
        )
        .scalars()
        .all()
    )
    for m in member_rows:
        if m.customer_id in seen:  # owner+成员并存的防御去重（owner 视角胜出）
            continue
        team = db.get(Customer, m.customer_id)
        if team is None:  # 成员行指向已删客户：跳过（解析层同款回退）
            continue
        entries.append((team.id, team.name, m.role))
        seen.add(team.id)
    own_entry = entries[0] if own is not None else None
    member_entries = sorted(entries[1:] if own is not None else entries, key=lambda e: e[0])
    entries = ([own_entry] if own_entry is not None else []) + member_entries
    # 活跃队判定（对齐 auth.resolve_member_role 解析序）
    active_id: int | None = None
    pointer = db.get(MemberActiveTeam, sub)
    if pointer is not None and pointer.customer_id in seen:
        active_id = pointer.customer_id
    if active_id is None and own is not None:
        active_id = own.id
    if active_id is None:
        for m in member_rows:
            if m.customer_id in seen:
                active_id = m.customer_id
                break
    if active_id is None:  # 依赖层已保证至少一队归属，此为类型兜底
        active_id = entries[0][0]
    return TeamsOut(
        teams=[
            TeamEntryOut(
                customer_id=cid,
                customer_name=name,
                role=role,
                is_active=cid == active_id,
            )
            for cid, name, role in entries
        ],
        active_customer_id=active_id,
    )


@router.post("/api/console/team/switch", response_model=TeamSwitchOut)
def switch_team(
    payload: TeamSwitchIn,
    identity: tuple[Customer, str] = Depends(console_auth.require_console_member_identity),
    db: Session = Depends(get_console_db),
) -> TeamSwitchOut:
    """切换活跃队（v2；个人面语义——任意已映射身份可切自己的指针）。

    - 目标须为自有队（logto_sub 命中）或该 (customer_id, sub) 存在成员行；
      否则 404（不泄露他队存在性）；
    - 目标已是活跃队 → 幂等成功（upsert 同值，仅刷新 updated_at）；
    - upsert member_active_team 指针行后即生效——解析层每次请求读库，
      无缓存失效面；
    - **不写 team_events**：个人 UI 态非团队变更（design D6）。
    """
    _customer, sub = identity
    own = console_auth.resolve_customer(db, sub)
    is_owner_of_target = own is not None and own.id == payload.customer_id
    member_row = (
        db.execute(
            select(CustomerMember).where(
                CustomerMember.customer_id == payload.customer_id,
                CustomerMember.logto_sub == sub,
            )
        )
        .scalar_one_or_none()
    )
    if not is_owner_of_target and member_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="目标团队不存在或无权访问",
        )
    pointer = db.get(MemberActiveTeam, sub)
    if pointer is None:
        db.add(MemberActiveTeam(logto_sub=sub, customer_id=payload.customer_id))
    else:
        pointer.customer_id = payload.customer_id
        pointer.updated_at = utcnow()
    db.commit()
    return TeamSwitchOut(active_customer_id=payload.customer_id)


@router.get("/api/console/team", response_model=TeamOut)
def get_team(
    owner: tuple[Customer, str] = Depends(console_auth.require_console_owner),
    db: Session = Depends(get_console_db),
) -> TeamOut:
    """成员名单（含隐含 owner 行）+ 待用邀请列表 + 席位用量（owner-only）。"""
    customer, caller_sub = owner
    member_rows = (
        db.execute(
            select(CustomerMember)
            .where(CustomerMember.customer_id == customer.id)
            .order_by(CustomerMember.created_at, CustomerMember.id)
        )
        .scalars()
        .all()
    )
    members = [
        # 隐含 owner 行：logto_sub=Customer.logto_sub（owner 不落成员表）
        TeamMemberOut(
            logto_sub=customer.logto_sub or "",
            role="owner",
            created_at=customer.created_at,
        )
    ]
    members.extend(
        TeamMemberOut(
            logto_sub=m.logto_sub,
            role=m.role,
            created_at=m.created_at,
            invited_by_sub=m.invited_by_sub,
        )
        for m in member_rows
    )
    invite_rows = (
        db.execute(
            select(CustomerInvite)
            .where(
                CustomerInvite.customer_id == customer.id,
                CustomerInvite.used_by_sub.is_(None),
                CustomerInvite.expires_at > utcnow(),
            )
            .order_by(CustomerInvite.id)
        )
        .scalars()
        .all()
    )
    return TeamOut(
        me=TeamMeOut(sub=caller_sub, role="owner"),
        members=members,
        invites=[
            TeamInviteOut(
                id=i.id,
                code=i.code,
                role=i.role,
                expires_at=_aware(i.expires_at),
                created_at=_aware(i.created_at),
            )
            for i in invite_rows
        ],
        seats_used=len(member_rows) + 1,
        seats_limit=seat_limit(db, _plan_code(customer)),
    )


@router.post(
    "/api/console/team/invites",
    response_model=TeamInviteOut,
    status_code=status.HTTP_201_CREATED,
)
def create_invite(
    payload: InviteCreateIn,
    owner: tuple[Customer, str] = Depends(console_auth.require_console_owner),
    db: Session = Depends(get_console_db),
) -> TeamInviteOut:
    """生成一次性邀请码（明文码只在本次响应出现；24h 过期）。

    席位闸（v2 4.2）：seats_used（成员行数+1）≥ seat_limit（档位）→
    409（detail 含档位、席位数字与升级指引），不产码——满员时生成的码
    无法兑换，从源头拒绝。
    """
    customer, caller_sub = owner
    gate_detail = _seat_full_detail(db, customer)
    if gate_detail is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=gate_detail)
    code = INVITE_CODE_PREFIX + secrets.token_hex(12)
    invite = CustomerInvite(
        customer_id=customer.id,
        code=code,
        role=payload.role,
        expires_at=utcnow() + INVITE_TTL,
        created_by_sub=caller_sub,
    )
    db.add(invite)
    _record_event(
        db,
        customer_id=customer.id,
        actor_sub=caller_sub,
        action="invite.created",
        target=code,
        detail={"role": payload.role},
    )
    db.commit()
    return TeamInviteOut(
        id=invite.id,
        code=invite.code,
        role=invite.role,
        expires_at=_aware(invite.expires_at),
        created_at=_aware(invite.created_at),
    )


@router.delete("/api/console/team/invites/{invite_id}", response_model=DeletedOut)
def revoke_invite(
    invite_id: int,
    owner: tuple[Customer, str] = Depends(console_auth.require_console_owner),
    db: Session = Depends(get_console_db),
) -> DeletedOut:
    """作废邀请（不存在 404；客户维度隔离——他企 id 一律 404 不泄露）。"""
    customer, caller_sub = owner
    invite = db.execute(
        select(CustomerInvite).where(
            CustomerInvite.id == invite_id,
            CustomerInvite.customer_id == customer.id,
        )
    ).scalar_one_or_none()
    if invite is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="邀请不存在")
    code = invite.code
    db.delete(invite)
    _record_event(
        db,
        customer_id=customer.id,
        actor_sub=caller_sub,
        action="invite.revoked",
        target=code,
    )
    db.commit()
    return DeletedOut()


@router.patch("/api/console/team/members/{sub}", response_model=TeamMemberOut)
def change_member_role(
    sub: str,
    payload: MemberRoleIn,
    owner: tuple[Customer, str] = Depends(console_auth.require_console_owner),
    db: Session = Depends(get_console_db),
) -> TeamMemberOut:
    """成员角色变更（v2 3.3；owner-only，editor ↔ viewer）。

    - sub = owner 本人 → 409「owner 角色不可经本端点变更」（owner 语义
      在解析层合成，不经成员面改写）；
    - 非**本队**成员行（customer_id 匹配当前解析的 customer.id）→ 404；
    - 同值幂等：直接 200 返回现角色，**不留痕**（无状态变更即无审计点）；
    - 变更：更新成员行 role + team_events('member.role_changed'，
      detail 含 {sub, old, new})；
    - 即时生效：解析层每次请求读库，下一次请求即按新角色门禁——无
      缓存失效面。
    """
    customer, caller_sub = owner
    if sub == customer.logto_sub:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="owner 角色不可经本端点变更",
        )
    member = db.execute(
        select(CustomerMember).where(
            CustomerMember.customer_id == customer.id,
            CustomerMember.logto_sub == sub,
        )
    ).scalar_one_or_none()
    if member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="成员不存在")
    if member.role != payload.role:
        old_role = member.role
        member.role = payload.role
        _record_event(
            db,
            customer_id=customer.id,
            actor_sub=caller_sub,
            action="member.role_changed",
            target=sub,
            detail={"sub": sub, "old": old_role, "new": payload.role},
        )
        db.commit()
    return TeamMemberOut(
        logto_sub=member.logto_sub,
        role=member.role,
        created_at=_aware(member.created_at),
        invited_by_sub=member.invited_by_sub,
    )


@router.delete("/api/console/team/members/{sub}", response_model=DeletedOut)
def remove_member(
    sub: str,
    request: Request,
    db: Session = Depends(get_console_db),
) -> DeletedOut:
    """撤销成员（owner-only；例外：成员自撤允许——只能删自己）。

    - 撤 owner 行 → 409「owner 不可被撤销」（owner 是客户创建者语义，
      不可经成员面移除）；
    - 不存在 → 404；
    - 成员删他人 → 403（团队管理仅 owner）；
    - **被撤者活跃队指针处置**（v2 多队，同事务）：member_active_team
      该 sub 指针指向本队时——若其另有成员行则重指其中 id 最小者，否则
      删指针行（此后解析层回退 owner/首队）；指针指向他队则不动。成员
      自撤同路径自然覆盖。
    """
    identity = console_auth.verify_id_token(console_auth.bearer_token(request))
    resolved = console_auth.resolve_member_role(db, identity.sub)
    if resolved is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录身份尚未映射到客户（请先完成首次登录开户）",
            headers={"WWW-Authenticate": "Bearer"},
        )
    customer, role = resolved
    if role != "owner" and sub != identity.sub:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="团队成员权限不足（团队管理仅 owner）",
        )
    if sub == customer.logto_sub:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="owner 不可被撤销",
        )
    member = db.execute(
        select(CustomerMember).where(
            CustomerMember.customer_id == customer.id,
            CustomerMember.logto_sub == sub,
        )
    ).scalar_one_or_none()
    if member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="成员不存在")
    db.delete(member)
    pointer = db.get(MemberActiveTeam, sub)
    if pointer is not None and pointer.customer_id == customer.id:
        remaining = (
            db.execute(
                select(CustomerMember)
                .where(
                    CustomerMember.logto_sub == sub,
                    CustomerMember.id != member.id,
                )
                .order_by(CustomerMember.id)
            )
            .scalars()
            .first()
        )
        if remaining is not None:
            pointer.customer_id = remaining.customer_id
            pointer.updated_at = utcnow()
        else:
            db.delete(pointer)
    _record_event(
        db,
        customer_id=customer.id,
        actor_sub=identity.sub,
        action="member.removed",
        target=sub,
        detail={"self": sub == identity.sub},
    )
    db.commit()
    return DeletedOut()
