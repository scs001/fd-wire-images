"""storage_pg 集成腿：真 PG 实例（env 门控，缺省整组 skip）。

门控：
- ``CELL_PG_TEST_ADMIN_URL`` 未设 → 整个模块 skip（真库腿需要真实例；
  本机直连示例：postgresql+psycopg://fd_cm_test:<pw>@<cheap3-host>:5433/postgres
  ——注意 fd_cm_test 对 wire_platform 无 CONNECT（baseline REVOKE 生效），
  管理连接用 ``postgres`` 库即可；若仍填了 /wire_platform，本模块会自动
  改写为 /postgres 并注明）。

凭据托管（remove-cell-openbao-dependency）：OpenBAO 退役后本腿直接用真
``FileSecretStore``（tmp 目录），不再有第二门 env 控外部凭据服务。

用后即删：全部对象挂在 ``wire_cell_test_<rand>`` 前缀下，teardown 兜底
DROP DATABASE/ROLE，不留残留。
"""

from __future__ import annotations

import os
import re
import uuid

import pytest

from cell_manager.secrets_store import FileSecretStore, SecretsStoreError
from cell_manager.storage_pg import PostgresStorage, StorageProvisionError

pytest.importorskip("psycopg")

_ADMIN_URL = os.environ.get("CELL_PG_TEST_ADMIN_URL", "")

pytestmark = pytest.mark.skipif(
    not _ADMIN_URL,
    reason="CELL_PG_TEST_ADMIN_URL 未设置——真 PG 集成腿关闭（单测/管理面桩不受影响）",
)


def _normalize_admin_url(url: str) -> str:
    """fd_cm_test 对 wire_platform 无 CONNECT（baseline REVOKE）——管理连接
    一律落到 postgres 库（集群级 DDL 与目录查询不挑库）。"""
    marker = "/wire_platform"
    if marker in url:
        replaced = url.replace(marker, "/postgres")
        print(f"[integration] admin URL {marker} → /postgres (baseline REVOKE 对测试管理员生效)")
        return replaced
    return url


@pytest.fixture
def admin_conn():
    import psycopg

    dsn = _normalize_admin_url(_ADMIN_URL)
    if dsn.startswith("postgresql+psycopg://"):
        dsn = "postgresql://" + dsn[len("postgresql+psycopg://"):]
    conn = psycopg.connect(dsn, autocommit=True)
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def drop_residue(admin_conn):
    """用后即删：记录本测试创建的库/角色，结束兜底强删。"""
    created: list[str] = []
    yield created
    for name in created:
        admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin_conn.execute(f'DROP ROLE IF EXISTS "{name}"')


def make_storage(customer_id: str, secrets: FileSecretStore) -> PostgresStorage:
    return PostgresStorage(
        admin_url=_normalize_admin_url(_ADMIN_URL),
        url_host=re.sub(r"^[^@]+@", "", _ADMIN_URL).split(":")[0],
        url_port="5433",
        secrets=secrets,
    )


def probe(admin_conn, kind: str, name: str) -> bool:
    table = "pg_roles" if kind == "role" else "pg_database"
    column = "rolname" if kind == "role" else "datname"
    return (
        admin_conn.execute(
            f"SELECT 1 FROM {table} WHERE {column} = %s", (name,)
        ).fetchone()
        is not None
    )


class TestRealProvisionLifecycle:
    def test_full_provision_isolation_teardown_and_destroy(
        self, admin_conn, drop_residue, tmp_path
    ):
        import psycopg

        cid = f"test_{uuid.uuid4().hex[:10]}"
        name = f"wire_cell_{cid}"
        drop_residue.append(name)
        store = FileSecretStore(tmp_path / "secrets")
        storage = make_storage(cid, store)

        # —— 七步开通（真库真 DDL）——
        url = storage.provision(cid)
        assert url.startswith(f"postgresql+psycopg://{name}:")
        assert url.endswith(f"@{storage._url_host}:5433/{name}")
        password = url.split(":", 3)[2].split("@")[0]
        assert re.match(r"^[0-9a-f]{48}$", password)

        # —— 隔离四查（真值；试点锚点输出）——
        checks = storage.report(cid)["isolation_checks"]
        print(f"[integration] isolation checks for {name}: {checks}")
        assert checks["owner->cell_db"] is True
        assert checks["owner->wire_platform"] is False
        if isinstance(checks.get("unrelated->cell_db"), bool):
            assert checks["unrelated->cell_db"] is False
            assert checks["unrelated->wire_platform"] is False
        else:
            print(f"[integration] unrelated-role probes skipped: {checks['unrelated->cell_db']}")

        # —— 属主凭据真连：本库进得去 ——
        owner_dsn = url.replace("postgresql+psycopg://", "postgresql://")
        with psycopg.connect(owner_dsn) as cell_conn:
            who = cell_conn.execute("select current_user, current_database()").fetchone()
        assert who == (name, name)

        # —— 属主凭据越库被拒（wire_platform）——
        denied = owner_dsn.rsplit("/", 1)[0] + "/wire_platform"
        with pytest.raises(psycopg.OperationalError, match="does not have CONNECT privilege"):
            psycopg.connect(denied)

        # —— 幂等重入：条目复用同一口令，URL 逐字相同 ——
        again = storage.provision(cid)
        assert again == url

        # —— destroy：库/角色/凭据条目全清 ——
        storage.destroy(cid)
        assert not probe(admin_conn, "database", name)
        assert not probe(admin_conn, "role", name)
        assert store.get(storage.secret_path(cid)) is None

    def test_self_heal_rotation_on_missing_entry(self, admin_conn, drop_residue, tmp_path):
        import psycopg

        cid = f"test_{uuid.uuid4().hex[:10]}"
        name = f"wire_cell_{cid}"
        drop_residue.append(name)
        store = FileSecretStore(tmp_path / "secrets")
        storage = make_storage(cid, store)

        first = storage.provision(cid)
        store.delete(storage.secret_path(cid))  # 模拟 v1 手工形态：角色在、条目缺
        second = storage.provision(cid)

        assert second != first  # 轮换出了新口令
        assert store.get(storage.secret_path(cid))["url"] == second
        with psycopg.connect(second.replace("postgresql+psycopg://", "postgresql://")):
            pass  # 新口令真实可登录（旧口令自此失效）
        storage.destroy(cid)
        assert not probe(admin_conn, "role", name)

    def test_teardown_created_drops_fresh_assets_on_failure(
        self, admin_conn, drop_residue, tmp_path
    ):
        cid = f"test_{uuid.uuid4().hex[:10]}"
        name = f"wire_cell_{cid}"
        drop_residue.append(name)

        class UnwritableStore(FileSecretStore):
            """托管写失败模拟（原「封印态」等价）：get 无条目、put 抛错。"""

            def get(self, path):
                return None

            def put(self, path, data):
                raise SecretsStoreError(
                    f"credential store put {path!r} failed "
                    "(OSError: [Errno 30] Read-only file system) — 核对目录"
                )

        storage = make_storage(cid, UnwritableStore(tmp_path / "secrets"))

        with pytest.raises(SecretsStoreError):
            storage.provision(cid)

        # 建库成功但凭据入托管失败：本次新建资产可经 teardown_created 回收
        assert probe(admin_conn, "database", name) or probe(admin_conn, "role", name)
        storage.teardown_created(cid)
        assert not probe(admin_conn, "database", name)
        assert not probe(admin_conn, "role", name)

    def test_invalid_customer_id_fails_before_touching_instance(self, admin_conn, tmp_path):
        storage = make_storage("Bad-ID", FileSecretStore(tmp_path / "secrets"))
        with pytest.raises(StorageProvisionError, match="lowercase alnum"):
            storage.provision("Bad-ID")
        assert not probe(admin_conn, "role", "wire_cell_Bad-ID")
