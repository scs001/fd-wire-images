"""DockerDriver：docker SDK 实现（惰性导入——本机无 docker 时 import 不炸）。

纪律：
- **模块级绝不 import docker**——``import cell_manager.drivers.docker`` 在
  未安装 docker SDK 的机器上必须成功（测试对此有断言）。
- SDK 缺失 / daemon 不可达都在 **实例化** 时转成 ``DriverError``（带修复指引），
  运行期操作失败同样包装为 ``DriverError`` 并链上原始异常。

部署注意（给上线时，README 详述）：
- cell-manager 容器需挂 docker.sock（或 DOCKER_HOST 指远程 daemon）
- bind mount 的 host 路径是 **daemon 侧** 路径——cell-manager 与 cell 卷
  必须在同一 daemon 可见卷面上（compose 模式同款约束）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cell_manager.drivers.base import (
    RUNNING,
    STOPPED,
    ContainerInfo,
    ContainerSpec,
)
from cell_manager.errors import DriverError

_STATUS_MAP = {"created": "created", "running": RUNNING, "exited": STOPPED, "dead": STOPPED, "restarting": RUNNING}


def _import_docker() -> Any:
    """惰性导入 docker SDK；缺失时抛带安装指引的 DriverError。"""
    try:
        import docker  # noqa: PLC0415 —— 刻意惰性：模块导入期零 docker 依赖
    except ImportError as exc:
        raise DriverError(
            "docker SDK not installed (pip install 'docker>=7.0' or use "
            "cell-manager[docker] extra); CELL_DRIVER=fake 可用于无 docker 演示"
        ) from exc
    return docker


class DockerDriver:
    """docker SDK 驱动（create 幂等语义对齐 FakeDriver：重名报错）。"""

    def __init__(self) -> None:
        docker = _import_docker()
        try:
            self._client = docker.from_env()
        except Exception as exc:  # docker.DockerException 等 —— 统一包装
            raise DriverError(
                f"docker daemon unreachable: {exc}（检查 docker.sock / DOCKER_HOST）"
            ) from exc

    # ---- 协议实现 ----

    def create(self, spec: ContainerSpec) -> ContainerInfo:
        for bind in spec.binds:
            if not Path(bind.host_path).exists():
                raise DriverError(f"bind source missing on daemon host: {bind.host_path}")
        try:
            self._client.containers.get(spec.name)
        except Exception:
            pass  # 不存在 → 走创建
        else:
            raise DriverError(f"container {spec.name!r} already exists")
        try:
            self._client.containers.create(
                image=spec.image,
                name=spec.name,
                environment=dict(spec.env),
                volumes=[f"{b.host_path}:{b.container_path}" for b in spec.binds],
                network=spec.network,
                restart_policy={"Name": "unless-stopped"},
                detach=True,
            )
        except Exception as exc:
            raise DriverError(f"docker create {spec.name!r} failed: {exc}") from exc
        return self.inspect(spec.name)

    def start(self, name: str) -> ContainerInfo:
        container = self._get(name)
        try:
            container.start()
        except Exception as exc:
            raise DriverError(f"docker start {name!r} failed: {exc}") from exc
        return self.inspect(name)

    def stop(self, name: str) -> ContainerInfo:
        container = self._get(name)
        try:
            container.stop()
        except Exception as exc:
            raise DriverError(f"docker stop {name!r} failed: {exc}") from exc
        return self.inspect(name)

    def wait_ready(self, name: str, *, timeout: float, poll_interval: float = 2.0) -> None:
        """等待容器健康（Docker HEALTHCHECK=healthy）；无健康检查则运行态即就绪。

        注册前的硬前置：registry 注册即触发安全扫描与健康探针，打在一个
        还没就绪的 cell 上会被判不可达并自动禁用（2026-10-07 生产实证——
        cell 镜像 start-period 25s，注册窗口必须等过它）。unhealthy 快速
        失败；超时抛 DriverError（带最后观测状态）。
        """
        import time  # noqa: PLC0415 —— 仅本方法用，避免模块级 import

        container = self._get(name)
        deadline = time.monotonic() + timeout
        last = "unknown"
        while True:
            try:
                container.reload()
                state = (container.attrs or {}).get("State", {})
            except Exception as exc:
                raise DriverError(f"docker inspect {name!r} failed: {exc}") from exc
            health = (state.get("Health") or {}).get("Status")
            if health == "healthy":
                return
            if health == "unhealthy":
                raise DriverError(
                    f"container {name!r} is unhealthy (HEALTHCHECK failing)"
                )
            if health is None:
                if state.get("Status") == "running":
                    return  # 无 HEALTHCHECK 配置：运行态即视为就绪（无法再深验）
                last = state.get("Status", "unknown")
            else:
                last = health
            if time.monotonic() >= deadline:
                raise DriverError(
                    f"container {name!r} not ready within {timeout}s (last health: {last})"
                )
            time.sleep(poll_interval)

    def remove(self, name: str, force: bool = False) -> None:
        container = self._get(name)
        try:
            container.remove(force=force)
        except Exception as exc:
            raise DriverError(f"docker remove {name!r} failed: {exc}") from exc

    def exists(self, name: str) -> bool:
        try:
            self._client.containers.get(name)
            return True
        except Exception:
            return False

    def inspect(self, name: str) -> ContainerInfo:
        container = self._get(name)
        try:
            attrs = container.attrs or {}
            cfg = attrs.get("Config", {})
            env_list = cfg.get("Env") or []
            env = {}
            for item in env_list:
                if "=" in item:
                    key, _, value = item.partition("=")
                    env[key] = value
            mounts = tuple(
                f"{m.get('Source', '')}:{m.get('Destination', '')}".split(":", 1)
                for m in attrs.get("Mounts", [])
                if m.get("Source") and m.get("Destination")
            )
            from cell_manager.drivers.base import BindMount  # noqa: PLC0415 —— 避免循环

            return ContainerInfo(
                name=name,
                status=_STATUS_MAP.get(attrs.get("State", {}).get("Status", ""), STOPPED),
                image=cfg.get("Image", ""),
                env=env,
                mounts=tuple(BindMount(host_path=h, container_path=c) for h, c in mounts),
            )
        except DriverError:
            raise
        except Exception as exc:
            raise DriverError(f"docker inspect {name!r} failed: {exc}") from exc

    # ---- 内部 ----

    def _get(self, name: str) -> Any:
        try:
            return self._client.containers.get(name)
        except Exception as exc:
            raise DriverError(f"no such container: {name!r} ({exc})") from exc
