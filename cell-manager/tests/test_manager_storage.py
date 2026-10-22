"""manager 接入 PG 画像（fake driver + PostgresStorage 打桩；无 PG、无网）。

覆盖契约清单：provision 覆盖 DAAS_DATABASE_URL、audit 字段白名单（无口令）、
失败回滚调 teardown_created、purge 调 destroy、deprovision 不调、
sqlite 画像行为与现状一致（对照）、旧 state 记录（无 storage 键）读侧默认。
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

import cell_manager.manager as manager_module
from cell_manager import (
    ACTIVE,
    CellManager,
    CellManagerConfig,
    ConfigError,
    FakeDriver,
    FakeRegistryClient,
)
from cell_manager.storage_pg import StorageProvisionError

PG_URL = "postgresql+psycopg://wire_cell_acme:pg-secret-pw@pg.internal:5433/wire_cell_acme"


def pg_env(home, **overrides) -> dict:
    env = {
        "CELL_MANAGER_HOME": str(home),
        "CELL_IMAGE": "fd-wire/cell-daas:test",
        "CELL_GATEWAY_URL": "https://wire.example.test/mcp",
        "CELL_REGISTRY_URL": "https://registry.example.test",
        "CELL_DRIVER": "fake",
        "CELL_REGISTRY_MODE": "fake",
        "CELL_STORAGE": "postgres",
        "CELL_PG_ADMIN_URL": "postgresql+psycopg://adm:admpw@pg.internal:5433/postgres",
        "CELL_PG_URL_HOST": "pg.internal",
        "CELL_PG_URL_PORT": "5433",
        "CELL_OPENBAO_ADDR": "http://openbao.internal:8200",
        "CELL_OPENBAO_TOKEN": "openbao-tok-1",
    }
    env.update(overrides)
    return env


class RecordingStorage:
    """PostgresStorage 桩：记录调用；可注入失败。"""

    last_instance: "RecordingStorage | None" = None

    def __init__(self, *, admin_url, url_host, url_port, openbao, path_prefix="wire/cell-db"):
        self.calls: list[tuple[str, str]] = []
        self.fail_provision = False
        self.url = PG_URL
        RecordingStorage.last_instance = self

    def provision(self, customer_id: str) -> str:
        self.calls.append(("provision", customer_id))
        if self.fail_provision:
            raise StorageProvisionError(
                "postgres storage step 'verify-isolation' failed for database 'wire_cell_acme'"
            )
        return self.url

    def teardown_created(self, customer_id: str) -> None:
        self.calls.append(("teardown_created", customer_id))

    def destroy(self, customer_id: str) -> None:
        self.calls.append(("destroy", customer_id))

    def secret_path(self, customer_id: str) -> str:
        return f"wire/cell-db/{customer_id}"

    def report(self, customer_id: str):
        return {
            "database": f"wire_cell_{customer_id}",
            "isolation_checks": {
                "owner->cell_db": True,
                "owner->wire_platform": False,
                "probe_role": "wire_cell_other",
                "unrelated->cell_db": False,
                "unrelated->wire_platform": False,
            },
        }


@pytest.fixture
def pg_storage(monkeypatch):
    """把 manager 里的 PostgresStorage / OpenbaoClient 换成桩（构造参数对齐）。

    返回委托代理：属性读写转发到最近构造的桩实例（manager __init__
    时构造），并清掉上个测试泄漏的类级 last_instance。
    """
    monkeypatch.setattr(manager_module, "PostgresStorage", RecordingStorage)
    monkeypatch.setattr(
        manager_module, "OpenbaoClient", lambda addr, token: ("fake-openbao", addr)
    )
    RecordingStorage.last_instance = None

    class _Proxy:
        def _instance(self) -> RecordingStorage:
            instance = RecordingStorage.last_instance
            assert instance is not None, "PostgresStorage stub not constructed yet"
            return instance

        def __getattr__(self, name):
            return getattr(self._instance(), name)

        def __setattr__(self, name, value):
            setattr(self._instance(), name, value)

    return _Proxy()


@pytest.fixture
def pg_manager(home, clock, pg_storage):
    config = CellManagerConfig.from_env(env=pg_env(home))
    manager = CellManager(
        driver=FakeDriver(), registry=FakeRegistryClient(), config=config, clock=clock
    )
    return manager


def read_audit(config) -> list[dict]:
    lines = config.audit_log.read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(line) for line in lines]


class TestProvisionPostgres:
    def test_overrides_daas_database_url_and_audits(self, pg_manager, pg_storage, home):
        status = pg_manager.provision("acme")

        assert status.state == ACTIVE
        # env：sqlite 缺省被 PG URL 覆盖（注入容器的就是凭据本身）
        info = pg_manager._driver.inspect("fd-wire-cell-acme")
        assert info.env["DAAS_DATABASE_URL"] == PG_URL
        # 审计：storage 画像字段齐备、无口令明文
        entries = read_audit(pg_manager._config)
        detail = entries[-1]["detail"]
        assert detail["storage"] == "postgres"
        assert detail["database"] == "wire_cell_acme"
        assert detail["openbao_path"] == "wire/cell-db/acme"
        assert "DAAS_DATABASE_URL" in detail["injected_credential_env"]
        assert detail["storage_isolation"]["owner->cell_db"] is True
        assert "pg-secret-pw" not in json.dumps(entries)
        # 簿记：storage 画像入 state record
        record = pg_manager._state.get("acme")
        assert record["storage"] == "postgres"

    def test_storage_failure_rolls_back_volume_and_teardown(self, pg_manager, pg_storage, home):
        pg_storage.fail_provision = True

        with pytest.raises(StorageProvisionError, match="verify-isolation"):
            pg_manager.provision("acme")

        # 原子回滚：容器无、卷删、簿记无、teardown_created 已调
        assert pg_manager._driver.names == []
        assert not (pg_manager._config.data_root / "acme").exists()
        assert pg_manager.status("acme").state == "not_found"
        assert ("teardown_created", "acme") in pg_storage.calls
        # 失败也留审计痕（错误形态与类型）
        entries = read_audit(pg_manager._config)
        assert entries[-1]["result"] == "error"
        assert entries[-1]["detail"]["type"] == "StorageProvisionError"
        assert "pg-secret-pw" not in json.dumps(entries)

    def test_driver_failure_after_storage_teardowns_created(self, home, clock, pg_storage):
        from cell_manager import DriverError

        class NeverReadyDriver(FakeDriver):
            def wait_ready(self, name, *, timeout):
                raise DriverError(f"container {name!r} not ready within {timeout}s")

        config = CellManagerConfig.from_env(env=pg_env(home))
        manager = CellManager(
            driver=NeverReadyDriver(), registry=FakeRegistryClient(), config=config, clock=clock
        )
        with pytest.raises(DriverError, match="not ready"):
            manager.provision("acme")
        # PG 侧已建（provision 已返回）→ 回滚必须回收本次新建资产
        assert ("provision", "acme") in pg_storage.calls
        assert ("teardown_created", "acme") in pg_storage.calls
        assert not (config.data_root / "acme").exists()


class TestPurgeAndDeprovision:
    def test_purge_destroys_database(self, pg_manager, pg_storage, clock):
        pg_manager.provision("acme")
        pg_manager.deprovision("acme")
        clock.advance(days=31)
        pg_manager.purge("acme")

        assert ("destroy", "acme") in pg_storage.calls

    def test_purge_expired_destroy_passed_through(self, pg_manager, pg_storage, clock):
        pg_manager.provision("acme")
        pg_manager.deprovision("acme")
        clock.advance(days=31)
        pg_manager.purge_expired()

        assert ("destroy", "acme") in pg_storage.calls

    def test_deprovision_keeps_database(self, pg_manager, pg_storage):
        pg_manager.provision("acme")
        pg_manager.deprovision("acme")

        storage_ops = [op for op, _ in pg_storage.calls if op != "provision"]
        assert storage_ops == []  # deprovision 不动库（保留期语义）


class TestSqliteProfileUnchanged:
    def test_sqlite_profile_builds_no_storage_and_behaves_as_before(
        self, home, clock, pg_storage, fake_driver, fake_registry
    ):
        config = CellManagerConfig.from_env(
            env={
                "CELL_MANAGER_HOME": str(home),
                "CELL_IMAGE": "fd-wire/cell-daas:test",
                "CELL_GATEWAY_URL": "https://wire.example.test/mcp",
                "CELL_REGISTRY_URL": "https://registry.example.test",
                "CELL_DRIVER": "fake",
                "CELL_REGISTRY_MODE": "fake",
            }
        )
        manager = CellManager(
            driver=fake_driver, registry=fake_registry, config=config, clock=clock
        )

        assert manager._storage is None  # postgres 分支零触达
        status = manager.provision("acme")
        assert status.state == ACTIVE
        info = fake_driver.inspect("fd-wire-cell-acme")
        assert info.env["DAAS_DATABASE_URL"] == "sqlite:////data/daas.db"
        # 审计 detail 与现状一致：无 PG 字段、无 DAAS_DATABASE_URL 记名
        detail = read_audit(config)[-1]["detail"]
        assert "storage" not in detail
        assert "database" not in detail
        assert "openbao_path" not in detail
        assert "DAAS_DATABASE_URL" not in detail["injected_credential_env"]
        # state record 仍记画像（值 sqlite）
        assert manager._state.get("acme")["storage"] == "sqlite"
        # 桩从未被实例化（sqlite 画像零触达）
        assert RecordingStorage.last_instance is None


class TestLegacyStateRecord:
    def test_old_record_without_storage_field_skips_destroy(self, home, clock, pg_storage):
        config = CellManagerConfig.from_env(env=pg_env(home))
        manager = CellManager(
            driver=FakeDriver(), registry=FakeRegistryClient(), config=config, clock=clock
        )
        # 手工落一份 v1 形态的 retention 记录（无 storage 键）
        manager._state.put(
            {
                "customer_id": "acme",
                "state": "retention",
                "container_name": "fd-wire-cell-acme",
                "volume_path": str(config.data_root / "acme"),
                "registry_path": "fd-wire-cell-acme",
                "image": config.image,
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "purge_at": "2026-01-02T00:00:00+00:00",
            }
        )

        manager.purge("acme")  # 读侧默认 sqlite → 不调 destroy

        assert not any(op == "destroy" for op, _ in pg_storage.calls)


class TestConfigValidation:
    def test_storage_must_be_known(self, home):
        with pytest.raises(ConfigError, match="CELL_STORAGE"):
            CellManagerConfig.from_env(env=pg_env(home, CELL_STORAGE="mysql"))

    def test_postgres_requires_all_five_envs_fail_closed(self, home):
        full = pg_env(home)
        for missing in (
            "CELL_PG_ADMIN_URL",
            "CELL_PG_URL_HOST",
            "CELL_PG_URL_PORT",
            "CELL_OPENBAO_ADDR",
            "CELL_OPENBAO_TOKEN",
        ):
            env = {k: v for k, v in full.items() if k != missing}
            with pytest.raises(ConfigError, match=missing):
                CellManagerConfig.from_env(env=env)

    def test_postgres_defaults_ok_and_sqlite_needs_nothing(self, home):
        config = CellManagerConfig.from_env(env=pg_env(home))
        assert config.storage == "postgres"
        assert config.openbao_path_prefix == "wire/cell-db"
        # sqlite：什么都不配也合法
        bare = CellManagerConfig.from_env(env={"CELL_MANAGER_HOME": str(home)})
        assert bare.storage == "sqlite"

    def test_port_must_be_numeric(self, home):
        with pytest.raises(ConfigError, match="CELL_PG_URL_PORT"):
            CellManagerConfig.from_env(env=pg_env(home, CELL_PG_URL_PORT="five-thousand"))


class TestStorageQuota:
    """wire-cell-storage-quota：CELL_PG_QUOTA_MB 下发（引擎侧裁决生效面）。"""

    def test_quota_env_injected_when_configured(self, home, clock, pg_storage):
        from cell_manager.drivers.fake import FakeDriver
        from tests.conftest import FakeRegistryClient

        config = CellManagerConfig.from_env(env=pg_env(home, CELL_PG_QUOTA_MB="512"))
        manager = CellManager(
            driver=FakeDriver(), registry=FakeRegistryClient(), config=config, clock=clock
        )
        manager.provision("acme")
        info = manager._driver.inspect("fd-wire-cell-acme")
        assert info.env["FD_CELL_STORAGE_QUOTA_MB"] == "512"

    def test_quota_env_absent_when_not_configured(self, pg_manager, pg_storage):
        pg_manager.provision("acme")
        info = pg_manager._driver.inspect("fd-wire-cell-acme")
        assert "FD_CELL_STORAGE_QUOTA_MB" not in info.env

    def test_quota_config_rejects_non_positive_and_non_integer(self, home):
        import pytest as _pytest
        from cell_manager.errors import ConfigError

        with _pytest.raises(ConfigError):
            CellManagerConfig.from_env(env=pg_env(home, CELL_PG_QUOTA_MB="0"))
        with _pytest.raises(ConfigError):
            CellManagerConfig.from_env(env=pg_env(home, CELL_PG_QUOTA_MB="abc"))
        with _pytest.raises(ConfigError):
            CellManagerConfig.from_env(env=pg_env(home, CELL_PG_QUOTA_MB="-5"))
        # 合法值与缺省
        cfg = CellManagerConfig.from_env(env=pg_env(home, CELL_PG_QUOTA_MB="128"))
        assert cfg.pg_quota_mb == 128
        assert CellManagerConfig.from_env(env=pg_env(home)).pg_quota_mb is None
