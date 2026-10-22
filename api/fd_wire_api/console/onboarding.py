"""首登自动开户 ceremony（wire-self-serve-launch 4.3，design D3）。

wire api 以服务身份内联触发既有开户 ceremony（cell-manager 构件语义不变，
只加触发入口）。步骤与断点（``console_onboardings.status`` 状态机）：

1. **受理**：``logto_sub`` 已映射客户 → 直接 ready（key_once=None）。
   否则落 ``pending`` 簿记行（每 sub 一行，数据库唯一约束兜底并发）。
2. **sub2api 代建**：``billing.ensure_customer``（幂等）→ Customer 行 +
   ``sub2api_user_id`` 回写。不可达 → fail-closed 中止（502，不留半开户：
   无映射的客户无法登录控制台，key 也未铸出）。
3. **registry 授权束**：caller-map 写入（username=Logto sub →
   sub2api_user_id；**失败即中止**，与 cell-manager grant 同纪律——不写
   这一条新客户从第一次入口调用起就被 402 CALLER_NOT_MAPPED 挡住）。
   cell 面（scope 文档 + 条目注册）仅付费档升档通道触发（免费档无 cell
   可指，跳过 cell 配给，见 4.6）。
4. **铸 key**：``POST /api/patch-keys``（admin 代铸通道，与 cell-manager
   AdminMintChannel 同契约）→ 明文仅本次响应一次性交付，簿记只落 key_id
   （``status=minted``）。
5. **映射落库**：``billing_customers.logto_sub = sub``（``status=done``）。

断点续作语义（任务 4.3 钉死）：
- 铸 key 前任一步失败 → 簿记停在 ``pending``，重试幂等续作（ensure_customer
  同名同档命中、registry upsert 幂等），不重复产生外部副作用；
- **铸 key 成功但映射失败**（进程崩在 4→5 之间）→ 簿记 ``minted``+key_id，
  重试**不再重复铸 key**（key_once=None，明文已随失败请求丢失，如需重交
  由 admin 治理通道另行铸发）；
- 全程成功 → 映射生效，重复调用 key_once 恒 None（不重复铸 key）。

残余风险（如实声明）：铸 key 请求超时但 registry 侧实际成功（at-least-once
窗口）时，重试会再铸一把——旧 key 未交付未映射，属可吊销的孤儿 key；
簿记 key_id 在此窗口尚未来得及落库，需经 registry 侧审计回查吊销。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..billing.customers import ensure_customer
from ..billing.models import Customer
from ..billing.sub2api import Sub2ApiClient, Sub2ApiError
from ..config import settings
from .db import init_db
from .models import ConsoleOnboarding

#: key 组快照与命名（cell_manager/config.py 的合成组约定，逐字一致；
#: customer_ref=wire 侧派生的 cell 短 id）
WIRE_CUSTOMERS_GROUP = "wire-customers"
CELL_GROUP_PREFIX = "wire-cust-"
CELL_SCOPE_PREFIX = "mcp-wire-cell-"
CELL_ENTRY_PREFIX = "fd-wire-cell-"

#: caller-map 写入端点（registry 切片二 3.1；与 CELL_PREFLIGHT_MAP_PATH 同径）
CALLER_MAP_PATH = "/api/sub2api-caller-map"
PATCH_KEYS_PATH = "/api/patch-keys"


class OnboardingError(Exception):
    """开户 ceremony 外部通道失败（registry 面；路由层转 502）。"""


def customer_ref(customer_id: int) -> str:
    """wire 侧 cell 短 id（cell_manager validate_customer_id 白名单内：
    字母数字开头，仅 [A-Za-z0-9._-]）。确定性派生，重试不漂移。"""
    return f"c{customer_id}"


def cell_group(customer_ref: str) -> str:
    return CELL_GROUP_PREFIX + customer_ref


def key_groups(customer_ref: str) -> list[str]:
    """客户 key 组快照：共享组（业务面）+ 合成组（自己的 cell scope）。"""
    return [WIRE_CUSTOMERS_GROUP, cell_group(customer_ref)]


class KeyMintChannel(Protocol):
    """铸 key 通道（与 cell_manager.keys.AdminMintChannel 同契约）。"""

    def mint(self, *, sub: str, groups: list[str], name: str) -> dict[str, Any]: ...


class RegistryFace(Protocol):
    """registry 授权束面（幂等 upsert + caller-map 写入）。"""

    def write_caller_map(self, username: str, value: str) -> None: ...

    def grant_cell_face(
        self,
        *,
        customer_ref: str,
        target_url: str,
        registry_path: str,
    ) -> None: ...


@dataclass(frozen=True)
class MintedKey:
    key_id: str
    key: str  # 明文，仅经本对象在响应中走一趟


class AdminKeyMintChannel:
    """registry admin 代铸通道（POST /api/patch-keys，AdminMintChannel 同径）。

    鉴权借 cell-manager 惯例 env ``CELL_REGISTRY_TOKEN``（平台 admin wgk-，
    Bearer + CSRF 免检）；registry 地址复用借用契约 ``REGISTRY_URL``。
    """

    def __init__(self, base_url: str, admin_token: str, *, timeout: float = 15.0) -> None:
        self._base = base_url.rstrip("/")
        self._admin_token = admin_token
        self._timeout = timeout

    def mint(self, *, sub: str, groups: list[str], name: str) -> dict[str, Any]:
        # registry 真契约（cell_manager AdminMintChannel 同径）：JSON 体，
        # groups 为列表（非 form、非逗号串——form 形状 registry 422）。
        try:
            resp = httpx.post(
                f"{self._base}{PATCH_KEYS_PATH}",
                json={"name": name, "username": sub, "groups": groups},
                headers={"Authorization": f"Bearer {self._admin_token}"},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise OnboardingError(f"registry 铸 key 不可达: {exc}") from exc
        if resp.status_code not in (200, 201):
            raise OnboardingError(
                f"registry 铸 key 失败: HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            doc = resp.json()
        except ValueError as exc:
            raise OnboardingError("registry 铸 key 响应非 JSON") from exc
        plaintext = doc.get("key")
        info = doc.get("info") or {}
        key_id = info.get("key_id") or doc.get("key_id")
        if not isinstance(plaintext, str) or not plaintext or not key_id:
            raise OnboardingError(
                f"patch-key mint response missing key/key_id: {sorted(doc)}"
            )
        return {"key_id": str(key_id), "key": plaintext}

    def revoke(self, key_id: str) -> str:
        """admin 旁路吊销（DELETE /api/patch-keys/{key_id}，cell_manager 同径）。

        返回 revoked | already_revoked | not_found；不可达抛 OnboardingError。
        """
        try:
            resp = httpx.delete(
                f"{self._base}{PATCH_KEYS_PATH}/{key_id}",
                headers={"Authorization": f"Bearer {self._admin_token}"},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise OnboardingError(f"registry 吊销 key 不可达: {exc}") from exc
        if resp.status_code == 404:
            return "not_found"
        if resp.status_code == 409:
            return "already_revoked"
        if resp.status_code != 200:
            raise OnboardingError(
                f"registry 吊销 key 失败: HTTP {resp.status_code}: {resp.text[:200]}"
            )
        return "revoked"


class HttpRegistryFace:
    """registry 授权束 HTTP 面（caller-map PUT + cell 面 upsert，幂等）。"""

    def __init__(self, base_url: str, admin_token: str, *, timeout: float = 15.0) -> None:
        self._base = base_url.rstrip("/")
        self._admin_token = admin_token
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._admin_token}"}

    def write_caller_map(self, username: str, value: str) -> None:
        try:
            resp = httpx.put(
                f"{self._base}{CALLER_MAP_PATH}",
                json={"username": username, "value": value},
                headers=self._headers(),
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise OnboardingError(f"registry caller-map 不可达: {exc}") from exc
        if resp.status_code not in (200, 201):
            raise OnboardingError(
                f"registry caller-map 写入失败: HTTP {resp.status_code}: {resp.text[:200]}"
            )

    def grant_cell_face(self, *, customer_ref: str, target_url: str, registry_path: str) -> None:
        """cell 面（付费档升档触发）：scope 文档 upsert + 条目注册 + 启用。

        命名与 cell-manager grant.py 逐字一致（合成组轨道不经 Logto）。
        """
        group = cell_group(customer_ref)
        scope_name = CELL_SCOPE_PREFIX + customer_ref
        scope_payload = {
            "scope_name": scope_name,
            "scope_type": "server_scope",
            "server_access": [{"server": registry_path, "methods": ["*"], "tools": ["*"]}],
            "group_mappings": [group],
            "ui_permissions": {"list_service": [registry_path]},
            "create_in_idp": False,
        }
        entry_fields = {
            "name": f"柏讯 cell · {customer_ref}",
            "description": "fd-wire cell entry (managed by wire-api onboarding)",
            "path": registry_path,
            "proxy_pass_url": target_url,
            "overwrite": "true",
            "visibility": "group-restricted",
            "allowed_groups": group,
        }
        self._post_form("/api/servers/register", entry_fields, ok=(200, 201))
        self._post_form(
            "/api/servers/toggle", {"path": registry_path, "new_state": "true"}, ok=(200,)
        )
        try:
            resp = httpx.post(
                f"{self._base}/api/servers/groups/import",
                json=scope_payload,
                headers=self._headers(),
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise OnboardingError(f"registry scope 导入不可达: {exc}") from exc
        if resp.status_code not in (200, 201):
            raise OnboardingError(
                f"registry scope 导入失败: HTTP {resp.status_code}: {resp.text[:200]}"
            )

    def _post_form(self, path: str, fields: dict[str, str], *, ok: tuple[int, ...]) -> None:
        try:
            resp = httpx.post(
                f"{self._base}{path}",
                data=fields,
                headers=self._headers(),
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise OnboardingError(f"registry {path} 不可达: {exc}") from exc
        if resp.status_code not in ok:
            raise OnboardingError(
                f"registry {path} 失败: HTTP {resp.status_code}: {resp.text[:200]}"
            )


def registry_admin_token() -> str:
    """registry admin 凭据（借 cell-manager 惯例 env；未配置 → 空）。"""
    return (os.getenv("CELL_REGISTRY_TOKEN") or "").strip()


def default_mint_channel() -> AdminKeyMintChannel:
    token = registry_admin_token()
    if not token:
        raise OnboardingError(
            "customer-key minting unavailable: set CELL_REGISTRY_TOKEN "
            "(registry admin on-behalf-of API)"
        )
    return AdminKeyMintChannel(settings.registry_url, token)


def default_registry_face() -> HttpRegistryFace:
    token = registry_admin_token()
    if not token:
        raise OnboardingError(
            "registry authorization face unavailable: set CELL_REGISTRY_TOKEN"
        )
    return HttpRegistryFace(settings.registry_url, token)


@dataclass
class OnboardingOutcome:
    """ceremony 结果：customer 恒有效；key_once 仅本次完成铸 key 时非 None。"""

    customer: Customer
    key_once: str | None


def onboard_console_customer(
    session: Session,
    sub2api: Sub2ApiClient,
    *,
    sub: str,
    display_name: str,
    plan_code: str = "free",
    mint_channel: KeyMintChannel | None = None,
    registry_face: RegistryFace | None = None,
    include_cell_face: bool = False,
    target_url_template: str = "http://fd-wire-cell-{ref}:8000/mcp",
) -> OnboardingOutcome:
    """首登自动开户（断点幂等；语义见模块 docstring）。

    ``include_cell_face``：付费档升档通道（4.6）触发 cell 授权面
    （scope 文档 + 条目注册 + 启用）；免费档跳过 cell 配给（无 cell 可指，
    只写 caller-map + 铸 key，key 组快照仍带合成组供升档后即期生效）。
    """
    init_db()
    # 1) 已映射客户：直接 ready（重复调用不重复铸 key）
    existing = session.execute(
        select(Customer).where(Customer.logto_sub == sub)
    ).scalar_one_or_none()
    if existing is not None:
        return OnboardingOutcome(customer=existing, key_once=None)

    # 2) 受理：断点簿记行（每 sub 一行；并发由唯一约束兜底）
    row = session.execute(
        select(ConsoleOnboarding).where(ConsoleOnboarding.logto_sub == sub)
    ).scalar_one_or_none()
    if row is None:
        row = ConsoleOnboarding(logto_sub=sub, status="pending")
        session.add(row)
        session.flush()

    # 3) sub2api 代建 + 本地 Customer 行（ensure_customer 幂等）
    if row.customer_id is None:
        try:
            customer, created = ensure_customer(
                session,
                sub2api,
                name=display_name,
                plan_code=plan_code,
                idempotency_key=f"console-onboarding:{sub}",
            )
        except Sub2ApiError as exc:
            session.rollback()
            raise
        row.customer_id = customer.id
        session.commit()
    else:
        customer = session.get(Customer, row.customer_id)
        if customer is None:  # 簿记在而客户行被手工清理：按幂等重建
            customer, _created = ensure_customer(
                session,
                sub2api,
                name=display_name,
                plan_code=plan_code,
                idempotency_key=f"console-onboarding:{sub}",
            )
            row.customer_id = customer.id
            session.commit()

    # 4) registry 授权束：caller-map 写入（失败即中止，不留不可用开户）
    face = registry_face if registry_face is not None else default_registry_face()
    ref = customer_ref(customer.id)
    face.write_caller_map(sub, customer.sub2api_user_id or "")
    if include_cell_face:
        face.grant_cell_face(
            customer_ref=ref,
            target_url=target_url_template.format(ref=ref),
            registry_path=CELL_ENTRY_PREFIX + ref,
        )

    # 5) 铸 key（仅 pending；minted 断点续作不重复铸）
    key_once: str | None = None
    if row.status == "pending":
        channel = mint_channel if mint_channel is not None else default_mint_channel()
        minted = channel.mint(
            sub=sub,
            groups=key_groups(ref),
            name=f"{ref}-customer-track",
        )
        row.status = "minted"
        row.key_id = minted["key_id"]  # 簿记只落 key_id，明文不落库
        session.commit()
        key_once = minted["key"]

    # 6) 映射落库（终态）
    if customer.logto_sub != sub:
        customer.logto_sub = sub
    row.status = "done"
    session.commit()
    return OnboardingOutcome(customer=customer, key_once=key_once)
