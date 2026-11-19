"""ContainerDriver 协议：容器生命周期原语（create/start/stop/remove/exists）。

manager 只依赖本协议——DockerDriver（生产）与 FakeDriver（测试/演示）
可替换；新增 Nomad/k8s 驱动时同样只实现本协议。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

#: 容器状态（驱动侧统一词表；docker created/running/exited 归一化）
CREATED = "created"
RUNNING = "running"
STOPPED = "stopped"


@dataclass(frozen=True)
class BindMount:
    """绑定挂载：host 目录 → 容器内路径（cell 卷唯一挂载原语）。"""

    host_path: str
    container_path: str


@dataclass(frozen=True)
class ContainerSpec:
    """创建容器的完整意图（manager 构造，驱动执行）。"""

    name: str
    image: str
    env: dict[str, str] = field(default_factory=dict)
    binds: tuple[BindMount, ...] = ()
    #: docker 网络名（None=驱动缺省）。wire cell 必须与 registry 栈同网，
    #: 注册条目的 target_url（http://<容器名>:8000/mcp）才能被网关解析。
    network: str | None = None


@dataclass(frozen=True)
class ContainerInfo:
    """容器现状快照（inspect 语义；mounts 暴露给隔离测试断言）。"""

    name: str
    status: str
    image: str
    env: dict[str, str] = field(default_factory=dict)
    mounts: tuple[BindMount, ...] = ()


@runtime_checkable
class ContainerDriver(Protocol):
    """容器驱动协议（规格 5.1：create/start/stop/remove/exists + inspect）。"""

    def create(self, spec: ContainerSpec) -> ContainerInfo:
        """创建（不启动）容器；重名即抛 DriverError。"""
        ...

    def start(self, name: str) -> ContainerInfo:
        """启动已存在的容器；已在运行则为幂等 no-op。"""
        ...

    def wait_ready(self, name: str, *, timeout: float) -> None:
        """等待容器进入就绪态（健康检查通过）；超时/不健康抛 DriverError。

        注册前的硬前置：registry 注册即触发安全扫描与健康探针，打在一个
        还没就绪的 cell 上会被判不可达并自动禁用（2026-10-07 生产实证）。
        """
        ...

    def stop(self, name: str) -> ContainerInfo:
        """停止运行中的容器（数据卷随宿主文件系统保留）。"""
        ...

    def remove(self, name: str, force: bool = False) -> None:
        """删除容器；运行中需 force=True（purge 用）。"""
        ...

    def exists(self, name: str) -> bool:
        ...

    def inspect(self, name: str) -> ContainerInfo:
        """返回现状快照；不存在抛 DriverError。"""
        ...
