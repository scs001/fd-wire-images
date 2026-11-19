"""cell 开通自助化（add-self-serve-cell-provision）的 api 侧切片。

两表落**平台库**（元数据库收敛开关优先，与 contact/admin 同库；生产走
alembic 0053，``init_db`` create_all 只是开发兜底）：

- ``customer_cell_endpoints``：自助配给链回写的客户 cell 入口注册表
  （customer_id 唯一）——``ai.executor.resolve_cell_endpoint`` 的 DB 兜底源；
- ``cell_provision_requests``：配给任务队列（customer_id 幂等唯一，
  queued|running|done|failed）——admin 改档入队，cheap1 cell-manager
  runner 经内部端点领任务/回写。

端点（``X-Internal-Token`` 闸，复用 demands 的 internal 依赖）：

- ``POST /internal/cell-provision/next``：领队头 queued 任务→running；无任务 204；
- ``POST /internal/cell-provision/{id}/result``：runner 回写 done（endpoint_url
  落 endpoints 表）/ failed（error 留痕，回退人工链）。

升档挂点在 ``admin.routes.patch_customer``（改档至付费档时 enqueue +
fire-and-forget 运营邮件）；admin 状态页经 ``GET /internal/admin/cell-provision``
只读列表。
"""

from __future__ import annotations

from .db import init_db, reset_cell_provision_runtime
from .routes import router

__all__ = ["init_db", "reset_cell_provision_runtime", "router"]
