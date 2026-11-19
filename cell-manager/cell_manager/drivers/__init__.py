"""容器驱动包：协议 + 工厂。"""

from __future__ import annotations

from cell_manager.config import CellManagerConfig
from cell_manager.drivers.base import (
    CREATED,
    RUNNING,
    STOPPED,
    BindMount,
    ContainerDriver,
    ContainerInfo,
    ContainerSpec,
)
from cell_manager.drivers.docker import DockerDriver
from cell_manager.drivers.fake import FakeDriver
from cell_manager.errors import ConfigError

__all__ = [
    "CREATED",
    "RUNNING",
    "STOPPED",
    "BindMount",
    "ContainerDriver",
    "ContainerInfo",
    "ContainerSpec",
    "DockerDriver",
    "FakeDriver",
    "build_driver",
]


def build_driver(config: CellManagerConfig) -> ContainerDriver:
    """按配置构造驱动（CELL_DRIVER=docker|fake）。

    fake 模式容器表落盘到 ``<home>/state/fake-containers.json``——CLI 跨进程
    演示时 Fake 容器与 cells.json 簿记同步存续（单测直构 FakeDriver 缺省纯内存）。
    """
    if config.driver_name == "fake":
        return FakeDriver(persist_path=config.home / "state" / "fake-containers.json")
    if config.driver_name == "docker":
        return DockerDriver()  # 实例化时才要求 SDK/daemon 可用
    raise ConfigError(f"unknown driver: {config.driver_name!r}")
