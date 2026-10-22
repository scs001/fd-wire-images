"""生命周期主路径：provision 幂等 / 状态流转 / 明确错误 / WAL / 未知客户。"""

from __future__ import annotations

import sqlite3

import pytest

from cell_manager import (
    ACTIVE,
    NOT_FOUND,
    PAUSED,
    CellManager,
    CellManagerConfig,
    CellManagerError,
    ConfigError,
    InvalidCellStateError,
    UnknownCellError,
)


class TestProvision:
    def test_provision_creates_volume_container_and_registry_entry(self, manager, fake_driver, fake_registry, config):
        status = manager.provision("acme")

        assert status.state == ACTIVE
        assert status.container_name == "fd-wire-cell-acme"
        assert status.registry_path == "fd-wire-cell-acme"
        # 卷：独立目录 + daas.db 就位
        volume = config.data_root / "acme"
        assert status.volume_path == str(volume)
        assert (volume / "daas.db").is_file()
        # 容器：创建并启动，env 注入隔离两件套
        info = fake_driver.inspect("fd-wire-cell-acme")
        assert info.status == "running"
        assert info.image == config.image
        assert info.env["DAAS_DATABASE_URL"] == "sqlite:////data/daas.db"
        assert info.env["WIRE_GATEWAY_URL"] == config.gateway_url
        # cell 画像（ADR-0001）：没有它引擎不装载 customer_dataset 组
        assert info.env["DAAS_PROFILE"] == "cell"
        # 与 registry 栈同网（网络名经 CELL_NETWORK 配置时才注入）
        assert info.env["WIRE_GATEWAY_URL"] == config.gateway_url
        # registry：server 条目已注册
        assert fake_registry.is_registered("fd-wire-cell-acme")

    def test_daas_db_initialized_as_empty_wal(self, manager, config):
        manager.provision("acme")
        conn = sqlite3.connect(config.data_root / "acme" / "daas.db")
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        assert str(mode).lower() == "wal"

    def test_provision_is_idempotent_when_active(self, manager, fake_driver, fake_registry):
        first = manager.provision("acme")
        again = manager.provision("acme")

        assert again.state == ACTIVE == first.state
        assert again.container_name == first.container_name
        # no-op 语义：不重复 create 容器、不重复注册条目
        assert fake_driver.call_count("create", "fd-wire-cell-acme") == 1
        assert fake_registry.calls.count(("register", "fd-wire-cell-acme")) == 1

    def test_provision_rejected_when_paused_with_clear_error(self, manager):
        manager.provision("acme")
        manager.pause("acme")
        with pytest.raises(InvalidCellStateError, match="paused.*resume"):
            manager.provision("acme")

    def test_provision_rejected_when_in_retention(self, manager):
        manager.provision("acme")
        manager.deprovision("acme")
        with pytest.raises(InvalidCellStateError, match="retention"):
            manager.provision("acme")

    def test_provision_rejects_unsafe_customer_id(self, manager):
        for bad in ("../escape", "/abs", ".", "..", "a/b", "", "空格 x", "x" * 65):
            with pytest.raises(CellManagerError):
                manager.provision(bad)

    def test_provision_injects_credential_envs_when_configured(self, home, fake_driver, fake_registry, clock):
        """8.3 G4：配置了凭据双轨时，provision 注入 WIRE_CUSTOMER_KEY /
        WIRE_PLATFORM_KEY，且不破坏既有隔离两件套。"""
        config = CellManagerConfig.from_env(
            env={
                "CELL_MANAGER_HOME": str(home),
                "CELL_IMAGE": "fd-wire/cell-daas:test",
                "CELL_GATEWAY_URL": "https://wire.example.test/mcp",
                "CELL_REGISTRY_URL": "https://registry.example.test",
                "CELL_DRIVER": "fake",
                "CELL_REGISTRY_MODE": "fake",
                "CELL_CUSTOMER_CREDENTIAL_MAP": '{"acme": "cust-key-1"}',
                "CELL_PLATFORM_CREDENTIAL": "plat-key-9",
            }
        )
        manager = CellManager(driver=fake_driver, registry=fake_registry, config=config, clock=clock)
        manager.provision("acme")

        info = fake_driver.inspect("fd-wire-cell-acme")
        assert info.env["WIRE_CUSTOMER_KEY"] == "cust-key-1"
        assert info.env["WIRE_PLATFORM_KEY"] == "plat-key-9"
        # 既有隔离两件套不受影响
        assert info.env["DAAS_DATABASE_URL"] == "sqlite:////data/daas.db"
        assert info.env["WIRE_GATEWAY_URL"] == config.gateway_url

    def test_provision_omits_credential_envs_when_unconfigured(self, manager, fake_driver):
        """8.3 G4：未配置凭据双轨时，provision 不注入这两个 env（cell 内
        代理 fail-closed，生产必须配置——见 README）。"""
        manager.provision("acme")

        info = fake_driver.inspect("fd-wire-cell-acme")
        assert "WIRE_CUSTOMER_KEY" not in info.env
        assert "WIRE_PLATFORM_KEY" not in info.env
        assert info.env["DAAS_DATABASE_URL"] == "sqlite:////data/daas.db"
        assert info.env["WIRE_GATEWAY_URL"]

    def test_customer_credential_map_must_be_json_object(self, home):
        """启动期 fail-fast：非法 JSON / 非对象 / 空 key 值都给 ConfigError。"""
        base = {
            "CELL_MANAGER_HOME": str(home),
            "CELL_DRIVER": "fake",
            "CELL_REGISTRY_MODE": "fake",
        }
        with pytest.raises(ConfigError, match="JSON object"):
            CellManagerConfig.from_env(env={**base, "CELL_CUSTOMER_CREDENTIAL_MAP": "not-json"})
        with pytest.raises(ConfigError, match="JSON object"):
            CellManagerConfig.from_env(env={**base, "CELL_CUSTOMER_CREDENTIAL_MAP": '["acme"]'})
        with pytest.raises(ConfigError, match="JSON object"):
            CellManagerConfig.from_env(
                env={**base, "CELL_CUSTOMER_CREDENTIAL_MAP": '{"acme": ""}'}
            )


class TestPauseResume:
    def test_pause_stops_container_and_keeps_volume(self, manager, fake_driver, config):
        manager.provision("acme")
        status = manager.pause("acme")

        assert status.state == PAUSED
        assert fake_driver.inspect("fd-wire-cell-acme").status == "stopped"
        # 数据保留：卷文件还在
        assert (config.data_root / "acme" / "daas.db").is_file()

    def test_resume_restarts_same_container_with_volume_intact(self, manager, fake_driver, config):
        manager.provision("acme")
        manager.pause("acme")
        # 暂停期间往卷里写文件（模拟数据留存）
        marker = config.data_root / "acme" / "customer-data.bin"
        marker.write_bytes(b"tenant-data")
        status = manager.resume("acme")

        assert status.state == ACTIVE
        assert fake_driver.inspect("fd-wire-cell-acme").status == "running"
        assert marker.read_bytes() == b"tenant-data"  # 原卷原样

    def test_pause_rejected_when_already_paused(self, manager):
        manager.provision("acme")
        manager.pause("acme")
        with pytest.raises(InvalidCellStateError, match="paused"):
            manager.pause("acme")

    def test_resume_rejected_when_active(self, manager):
        manager.provision("acme")
        with pytest.raises(InvalidCellStateError, match="active"):
            manager.resume("acme")


class TestStatusUnknown:
    def test_status_unknown_customer_is_not_found(self, manager):
        status = manager.status("ghost-corp")
        assert status.state == NOT_FOUND
        assert status.to_dict()["state"] == "not_found"

    def test_operations_on_unknown_customer_raise(self, manager):
        for op in (manager.pause, manager.resume, manager.deprovision, manager.cancel_deprovision):
            with pytest.raises(UnknownCellError, match="ghost-corp"):
                op("ghost-corp")


class TestProvisionReadiness:
    """注册前等就绪 + 原子回滚（wire-native-channel-cutover 实证修复）。

    registry 注册即触发安全扫描与健康探针；打在一个还没就绪的 cell 上会
    被判不可达并自动禁用（2026-10-07 生产实证），故 provision 的
    start→register 之间必须等健康；创建之后的任何失败原子回滚（不留孤儿）。
    """

    def test_waits_ready_before_register(self, config, clock):
        order: list[str] = []

        from cell_manager import FakeDriver as _FakeDriver
        from cell_manager import FakeRegistryClient as _FakeRegistry

        class OrderingDriver(_FakeDriver):
            def wait_ready(self, name, *, timeout):
                order.append("wait_ready")
                super().wait_ready(name, timeout=timeout)

        class OrderingRegistry(_FakeRegistry):
            def register(self, entry):
                order.append("register")
                super().register(entry)

        manager = CellManager(
            driver=OrderingDriver(),
            registry=OrderingRegistry(),
            config=config,
            clock=clock,
        )
        manager.provision("acme")
        assert order == ["wait_ready", "register"]

    def test_wait_ready_failure_rolls_back_atomically(self, config, fake_registry, clock):
        from cell_manager import DriverError
        from cell_manager import FakeDriver as _FakeDriver

        class NeverReadyDriver(_FakeDriver):
            def wait_ready(self, name, *, timeout):
                raise DriverError(
                    f"container {name!r} not ready within {timeout}s (last health: starting)"
                )

        driver = NeverReadyDriver()
        manager = CellManager(driver=driver, registry=fake_registry, config=config, clock=clock)
        with pytest.raises(DriverError, match="not ready"):
            manager.provision("acme")
        # 原子回滚：容器删、卷删、未注册、簿记无记录（可干净重跑）
        assert driver.names == []
        assert not (config.data_root / "acme").exists()
        assert fake_registry.paths == []
        assert manager.status("acme").state == NOT_FOUND
        # 审计留痕：error 条目在案
        audit_lines = config.audit_log.read_text(encoding="utf-8").strip().splitlines()
        assert any('"action": "provision"' in line and '"result": "error"' in line for line in audit_lines)
        # 重跑（驱动恢复就绪）成功——回滚后不留脏状态
        ok_driver = _FakeDriver()
        manager2 = CellManager(driver=ok_driver, registry=fake_registry, config=config, clock=clock)
        assert manager2.provision("acme").state == ACTIVE

    def test_readiness_timeout_config_validation(self, home):
        with pytest.raises(ConfigError, match="CELL_READINESS_TIMEOUT"):
            CellManagerConfig.from_env(
                env={"CELL_MANAGER_HOME": str(home), "CELL_READINESS_TIMEOUT": "soon"}
            )
        with pytest.raises(ConfigError, match="CELL_READINESS_TIMEOUT"):
            CellManagerConfig.from_env(
                env={"CELL_MANAGER_HOME": str(home), "CELL_READINESS_TIMEOUT": "0"}
            )
