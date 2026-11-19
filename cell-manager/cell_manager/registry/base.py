"""RegistryClient 协议：cell 的 registry server 条目注册/摘除（可 mock）。

wire 入口（registry fork）的 server 条目是 cell 可达性的门面——
provision 即注册（同门计量，ADR-0001）、purge 即摘除（任务 5.2 联调真实 API）。

add-customer-onboarding-automation 起，协议扩为「条目 + 授权」两面：
条目带组受限可见性（发现面），scope 文档随授权束铸造/回收（调用面）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ServerEntry:
    """registry server 条目（path 为 wire 入口内的挂载路径）。

    visibility/allowed_groups 是发现面隔离（registry 既有 Form 字段）：
    cell 条目一律 group-restricted + 只放行本客户合成组——search/ans 类
    surface 按文档可见性过滤；listing 主路径随 scope 推导，两把锁互补。
    """

    path: str
    target_url: str
    display_name: str | None = None
    visibility: str = "public"
    allowed_groups: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, str]:
        payload = {"path": self.path, "target_url": self.target_url}
        if self.display_name:
            payload["display_name"] = self.display_name
        if self.allowed_groups:
            payload["visibility"] = self.visibility
            payload["allowed_groups"] = ",".join(self.allowed_groups)
        return payload


@dataclass(frozen=True)
class ScopeDoc:
    """per-customer scope 文档（cell 授权束的调用面半边）。

    形状即 registry ``POST /api/servers/groups/import`` 的一次性全量写入：
    server_access 精确名匹配 cell 条目、methods/tools 全通配；group_mappings
    只挂本客户合成组（不经 IdP，ADR 0005）。scope_type 固定 server_scope。
    """

    scope_name: str
    #: server_access 的精确条目名（= cell 条目 path）
    server: str
    #: group_mappings 的唯一组（= 客户合成组）
    group: str
    methods: tuple[str, ...] = field(default=("all",))
    tools: tuple[str, ...] = field(default=("all",))


@runtime_checkable
class RegistryClient(Protocol):
    """registry 借用契约（HTTP 形态见 http.py；测试用 fake.py）。"""

    def register(self, entry: ServerEntry) -> None:
        """注册（幂等：同 path 同 target 重复注册为 no-op）。"""
        ...

    def unregister(self, path: str) -> bool:
        """摘除；返回是否真摘除了条目（不存在时 False，不报错）。"""
        ...

    def upsert_scope(self, scope: ScopeDoc) -> None:
        """铸造/更新授权 scope 文档（幂等 upsert；不触碰 IdP）。"""
        ...

    def delete_scope(self, scope_name: str) -> bool:
        """删除授权 scope 文档；返回是否真删了（不存在时 False，不报错）。"""
        ...
