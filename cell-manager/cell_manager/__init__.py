"""cell-manager：柏讯平台企业 cell 生命周期管理（wire-platform-v1 任务 5.1）。

cell = 企业（ADR-0002）：一个隔离容器实例 + 一个独立 SQLite WAL 数据卷。
本包提供::

    库     from cell_manager import CellManager, provision, pause, ...
    CLI    python -m cell_manager provision|pause|resume|deprovision|
                     cancel-deprovision|status|purge-expired <customer_id>

核心零第三方依赖（stdlib）；docker SDK 为可选 extra（``pip install
'.[docker]'``），缺失时 import 本包不炸、实例化 DockerDriver 才报错。
"""

from cell_manager import audit, config, db, drivers, errors, grant, keys, registry, state
from cell_manager.audit import AuditLog
from cell_manager.config import CellManagerConfig
from cell_manager.drivers import (
    BindMount,
    ContainerDriver,
    ContainerInfo,
    ContainerSpec,
    DockerDriver,
    FakeDriver,
    build_driver,
)
from cell_manager.errors import (
    CellManagerError,
    ConfigError,
    DriverError,
    InvalidCellStateError,
    InvalidCustomerIdError,
    RegistryError,
    UnknownCellError,
)
from cell_manager.grant import CellGrant, decommission
from cell_manager.manager import (
    NOT_FOUND,
    CellManager,
    CellStatus,
)
from cell_manager.registry import (
    FakeRegistryClient,
    HttpRegistryClient,
    RegistryClient,
    ScopeDoc,
    ServerEntry,
    build_registry_client,
)
from cell_manager.state import ACTIVE, PAUSED, RETENTION, StateStore

__version__ = "0.1.0"

__all__ = [
    # 配置 / 错误
    "CellManagerConfig",
    "CellManagerError",
    "ConfigError",
    "DriverError",
    "InvalidCellStateError",
    "InvalidCustomerIdError",
    "RegistryError",
    "UnknownCellError",
    # 驱动
    "BindMount",
    "ContainerDriver",
    "ContainerInfo",
    "ContainerSpec",
    "DockerDriver",
    "FakeDriver",
    "build_driver",
    # registry
    "FakeRegistryClient",
    "HttpRegistryClient",
    "RegistryClient",
    "ScopeDoc",
    "ServerEntry",
    "build_registry_client",
    # 授权束（add-customer-onboarding-automation）
    "CellGrant",
    "decommission",
    "default_grant_manager",
    # 管理面
    "AuditLog",
    "CellManager",
    "CellStatus",
    "StateStore",
    "NOT_FOUND",
    "ACTIVE",
    "PAUSED",
    "RETENTION",
    # 库函数（与 CLI 同名）
    "default_manager",
    "provision",
    "pause",
    "resume",
    "deprovision",
    "cancel_deprovision",
    "purge_expired",
    "status",
    "__version__",
]


def default_manager() -> CellManager:
    """按 env 构造管理面（CELL_DRIVER / CELL_REGISTRY_MODE / CELL_* 配置生效）。"""
    cfg = CellManagerConfig.from_env()
    return CellManager(
        driver=build_driver(cfg),
        registry=build_registry_client(cfg),
        config=cfg,
    )


def default_grant_manager() -> CellGrant:
    """按 env 构造授权束编排面（与 default_manager 共配置/审计/簿记）。"""
    cfg = CellManagerConfig.from_env()
    return CellGrant(registry=build_registry_client(cfg), config=cfg)


def provision(customer_id: str) -> CellStatus:
    """开通企业 cell（幂等；详见 CellManager.provision）。"""
    return default_manager().provision(customer_id)


def pause(customer_id: str) -> CellStatus:
    """暂停企业 cell（容器停、数据卷保留）。"""
    return default_manager().pause(customer_id)


def resume(customer_id: str) -> CellStatus:
    """恢复已暂停的企业 cell（原卷重启）。"""
    return default_manager().resume(customer_id)


def deprovision(customer_id: str) -> CellStatus:
    """申请注销：进入保留期（CELL_RETENTION_DAYS，默认 30 天）。"""
    return default_manager().deprovision(customer_id)


def cancel_deprovision(customer_id: str) -> CellStatus:
    """保留期内撤销注销、恢复 ACTIVE。"""
    return default_manager().cancel_deprovision(customer_id)


def purge_expired() -> list[CellStatus]:
    """清除全部到期 retention cell（后台定时任务调此入口）。"""
    return default_manager().purge_expired()


def status(customer_id: str) -> CellStatus:
    """查询 cell 状态：not_found / active / paused / retention(+剩余天数)。"""
    return default_manager().status(customer_id)
