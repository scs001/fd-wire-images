"""驱动与 registry：Docker 惰性导入纪律 / FakeDriver 语义 / FakeClient 契约。"""

from __future__ import annotations

import sys
import types

import pytest

from cell_manager import (
    BindMount,
    ConfigError,
    ContainerSpec,
    DriverError,
    FakeDriver,
    FakeRegistryClient,
    RegistryError,
    ServerEntry,
)
from cell_manager.drivers import build_driver
from cell_manager.drivers.base import ContainerDriver
from cell_manager.drivers.docker import DockerDriver, _import_docker
from cell_manager.registry.base import RegistryClient


def _spec(name: str = "fd-wire-cell-acme") -> ContainerSpec:
    return ContainerSpec(
        name=name,
        image="img:test",
        env={"DAAS_DATABASE_URL": "sqlite:////data/daas.db"},
        binds=(BindMount(host_path="/tmp/acme", container_path="/data"),),
    )


class TestDockerDriverImportSafety:
    def test_module_imports_without_docker_sdk(self, monkeypatch):
        # docker SDK 未装（本机即如此）：模块导入必须不炸——
        # reload 重新执行模块体，若有人把 import docker 提到模块级即在此失败
        monkeypatch.setitem(sys.modules, "docker", None)
        import importlib

        import cell_manager.drivers.docker as mod

        fresh = importlib.reload(mod)
        assert hasattr(fresh, "DockerDriver")

    def test_instantiation_raises_clear_error_when_sdk_missing(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "docker", None)
        with pytest.raises(DriverError, match="docker SDK not installed"):
            DockerDriver()

    def test_instantiation_raises_when_daemon_unreachable(self, monkeypatch):
        fake_docker = types.ModuleType("docker")

        class _DockerException(Exception):
            pass

        def _from_env():
            raise _DockerException("Cannot connect to the Docker daemon")

        fake_docker.DockerException = _DockerException
        fake_docker.from_env = _from_env
        monkeypatch.setitem(sys.modules, "docker", fake_docker)
        with pytest.raises(DriverError, match="docker daemon unreachable"):
            DockerDriver()


class TestFakeDriverSemantics:
    def test_protocol_conformance(self):
        driver = FakeDriver()
        assert isinstance(driver, ContainerDriver)

    def test_create_does_not_start(self):
        driver = FakeDriver()
        info = driver.create(_spec())
        assert info.status == "created"

    def test_create_duplicate_name_raises(self):
        driver = FakeDriver()
        driver.create(_spec())
        with pytest.raises(DriverError, match="already exists"):
            driver.create(_spec())

    def test_stop_is_idempotent_and_keeps_container(self):
        driver = FakeDriver()
        driver.create(_spec())
        driver.start("fd-wire-cell-acme")
        info = driver.stop("fd-wire-cell-acme")
        assert info.status == "stopped"
        again = driver.stop("fd-wire-cell-acme")
        assert again.status == "stopped"
        assert driver.exists("fd-wire-cell-acme")

    def test_remove_running_requires_force(self):
        driver = FakeDriver()
        driver.create(_spec())
        driver.start("fd-wire-cell-acme")
        with pytest.raises(DriverError, match="force"):
            driver.remove("fd-wire-cell-acme")
        driver.remove("fd-wire-cell-acme", force=True)
        assert not driver.exists("fd-wire-cell-acme")

    def test_unknown_container_operations_raise(self):
        driver = FakeDriver()
        for op in (lambda: driver.start("nope"), lambda: driver.stop("nope"), lambda: driver.inspect("nope")):
            with pytest.raises(DriverError, match="no such container"):
                op()

    def test_inspect_exposes_env_and_mounts(self):
        driver = FakeDriver()
        info = driver.create(_spec())
        assert info.env["DAAS_DATABASE_URL"] == "sqlite:////data/daas.db"
        assert info.mounts == (BindMount(host_path="/tmp/acme", container_path="/data"),)


class TestDriverFactory:
    def test_build_driver_fake(self, config):
        assert isinstance(build_driver(config), FakeDriver)

    def test_build_driver_unknown_raises(self, config):
        from dataclasses import replace

        with pytest.raises(ConfigError):
            build_driver(replace(config, driver_name="nomad"))


class TestFakeRegistryClient:
    def test_protocol_conformance(self):
        assert isinstance(FakeRegistryClient(), RegistryClient)

    def test_register_and_unregister(self):
        client = FakeRegistryClient()
        client.register(ServerEntry(path="fd-wire-cell-acme", target_url="http://x/mcp"))
        assert client.is_registered("fd-wire-cell-acme")

        assert client.unregister("fd-wire-cell-acme") is True
        assert not client.is_registered("fd-wire-cell-acme")
        # 再摘一次：不存在 → False 宽松（purge 重入安全）
        assert client.unregister("fd-wire-cell-acme") is False

    def test_register_idempotent_for_same_target(self):
        client = FakeRegistryClient()
        entry = ServerEntry(path="p", target_url="http://x/mcp")
        client.register(entry)
        client.register(entry)  # no-op，不报错
        assert client.entry("p") == entry

    def test_register_conflicting_target_raises(self):
        client = FakeRegistryClient()
        client.register(ServerEntry(path="p", target_url="http://x/mcp"))
        with pytest.raises(RegistryError, match="already registered"):
            client.register(ServerEntry(path="p", target_url="http://y/mcp"))


class TestDockerDriverWaitReady:
    """wait_ready：注册前硬前置（registry 注册即触发扫描/探针，未就绪即被
    自动禁用——10-07 生产实证）。三态：healthy 返回 / unhealthy 快速失败 /
    超时失败；无 HEALTHCHECK 时运行态即就绪。"""

    def _driver(self, monkeypatch, health_sequence, *, status="running"):
        fake_docker = types.ModuleType("docker")

        class _DockerException(Exception):
            pass

        class FakeContainer:
            def __init__(self):
                self._seq = list(health_sequence)

            def reload(self):
                pass

            @property
            def attrs(self):
                h = self._seq.pop(0) if len(self._seq) > 1 else self._seq[0]
                state = {"Status": status}
                if h is not None:
                    state["Health"] = {"Status": h}
                return {"State": state, "Config": {}, "Mounts": []}

        container = FakeContainer()

        class FakeContainers:
            def get(self, name):
                return container

        class FakeClient:
            containers = FakeContainers()

        fake_docker.DockerException = _DockerException
        fake_docker.from_env = lambda: FakeClient()
        monkeypatch.setitem(sys.modules, "docker", fake_docker)
        return DockerDriver()

    def test_returns_after_healthy(self, monkeypatch):
        driver = self._driver(monkeypatch, ["starting", "starting", "healthy"])
        driver.wait_ready("cell-1", timeout=5, poll_interval=0.01)

    def test_unhealthy_fails_fast(self, monkeypatch):
        driver = self._driver(monkeypatch, ["starting", "unhealthy"])
        with pytest.raises(DriverError, match="unhealthy"):
            driver.wait_ready("cell-1", timeout=5, poll_interval=0.01)

    def test_timeout_reports_last_status(self, monkeypatch):
        driver = self._driver(monkeypatch, ["starting"])
        with pytest.raises(DriverError, match="not ready within .*last health: starting"):
            driver.wait_ready("cell-1", timeout=0.05, poll_interval=0.01)

    def test_no_healthcheck_running_is_ready(self, monkeypatch):
        driver = self._driver(monkeypatch, [None])
        driver.wait_ready("cell-1", timeout=1, poll_interval=0.01)
