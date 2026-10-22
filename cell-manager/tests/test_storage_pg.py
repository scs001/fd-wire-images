"""storage_pg 单元（无 PG、无网）：psycopg 连接替身 + OpenBAO 替身。

覆盖契约清单：七步成功（URL 形态、口令只进局部与 OpenBAO）、幂等重入
（复用条目不 ALTER）、自愈轮换（角色在、条目缺 → ALTER+put）、隔离四查
失败（fail-closed、回滚不误删既有资产）、teardown_created 只删本次新建、
destroy 幂等、psycopg 缺失指引、标识符白名单。
"""

from __future__ import annotations

import re
import sys

import pytest

import cell_manager.storage_pg as storage_pg
from cell_manager.openbao import OpenbaoError
from cell_manager.storage_pg import PostgresStorage, StorageProvisionError

ADMIN_URL = "postgresql+psycopg://adm:admpw@pg.internal:5433/postgres"
URL_HOST = "db-internal.cheap-3"
URL_PORT = "5433"
PROBE_ROLE = "wire_cell_someone_else"
HEX48_RE = re.compile(r"^[0-9a-f]{48}$")


class FakeResult:
    def __init__(self, row) -> None:
        self._row = row

    def fetchone(self):
        return self._row


class FakePgState:
    """共享假集群：角色/库集合 + has_database_privilege 的真值表。"""

    def __init__(self, *, roles=(), dbs=(), probe_role=PROBE_ROLE, is_super=False) -> None:
        self.roles = set(roles)
        self.dbs = set(dbs)
        self.passwords: dict[str, str] = {}
        self.probe_role = probe_role
        self.is_super = is_super
        self.executed: list[tuple[str, tuple | None]] = []
        self.privileges: dict[tuple[str, str], bool] = {}

    def default_isolation(self, role: str, db: str) -> None:
        """四查默认真值：属主→本库 t，其余 f（wire_platform 恒不在授权面）。"""
        self.privileges.update(
            {
                (role, db): True,
                (role, "wire_platform"): False,
                (PROBE_ROLE, db): False,
                (PROBE_ROLE, "wire_platform"): False,
            }
        )

    def sql(self, verb: str) -> list[tuple[str, tuple | None]]:
        return [(s, p) for s, p in self.executed if s.strip().upper().startswith(verb)]


class FakePgConnection:
    """psycopg Connection 替身：按 SQL 特征路由（storage_pg 使用的全部语句）。"""

    def __init__(self, state: FakePgState) -> None:
        self.state = state
        self.closed = False

    def execute(self, sql: str, params=None):  # noqa: ANN201 - 与 psycopg 协议对齐
        s = sql.strip()
        self.state.executed.append((sql, params))
        # 顺序敏感：探测无关角色的查询也含 rolsuper/rolname，最特异的先判
        if s.upper().startswith("SELECT ROLSUPER"):
            return FakeResult((self.state.is_super,))
        if "rolcanlogin" in s and "LIMIT 1" in s:
            if self.state.probe_role:
                return FakeResult((self.state.probe_role,))
            return FakeResult(None)
        if "FROM pg_roles WHERE rolname" in s:
            role = params[0]
            return FakeResult((1,) if role in self.state.roles else None)
        if "FROM pg_database WHERE datname" in s:
            db = params[0]
            return FakeResult((1,) if db in self.state.dbs else None)
        if "rolcanlogin" in s and "LIMIT 1" in s:
            if self.state.probe_role:
                return FakeResult((self.state.probe_role,))
            return FakeResult(None)
        if "has_database_privilege" in s:
            grantee, target = params
            return FakeResult((self.state.privileges.get((grantee, target), False),))
        upper = s.upper()
        if upper.startswith("CREATE ROLE"):
            name = re.search(r'CREATE ROLE "([^"]+)"', s).group(1)
            self.state.roles.add(name)
            self.state.passwords[name] = params[0]
            return FakeResult(None)
        if upper.startswith("CREATE DATABASE"):
            name = re.search(r'CREATE DATABASE "([^"]+)"', s).group(1)
            self.state.dbs.add(name)
            return FakeResult(None)
        if upper.startswith("GRANT"):
            return FakeResult(None)
        if upper.startswith("ALTER ROLE"):
            name = re.search(r'ALTER ROLE "([^"]+)"', s).group(1)
            self.state.passwords[name] = params[0]
            return FakeResult(None)
        if upper.startswith("REVOKE"):
            return FakeResult(None)
        if upper.startswith("DROP DATABASE"):
            name = re.search(r'DROP DATABASE (?:IF EXISTS )?"([^"]+)"', s).group(1)
            self.state.dbs.discard(name)
            return FakeResult(None)
        if upper.startswith("DROP ROLE"):
            name = re.search(r'DROP ROLE (?:IF EXISTS )?"([^"]+)"', s).group(1)
            self.state.roles.discard(name)
            self.state.passwords.pop(name, None)
            return FakeResult(None)
        raise AssertionError(f"FakePgConnection: unexpected SQL: {sql!r}")

    def close(self) -> None:
        self.closed = True


class FakeOpenbao:
    def __init__(self, entries: dict | None = None, *, fail_put=False) -> None:
        self.entries: dict[str, dict] = dict(entries or {})
        self.put_calls: list[tuple[str, dict]] = []
        self.delete_calls: list[str] = []
        self.fail_put = fail_put

    def put(self, path, data):
        if self.fail_put:
            raise OpenbaoError("OpenBAO PUT secret/data/x failed (HTTP 503) — 先解封 OpenBAO")
        self.put_calls.append((path, dict(data)))
        self.entries[path] = dict(data)

    def get(self, path):
        return self.entries.get(path)

    def delete(self, path):
        self.delete_calls.append(path)
        self.entries.pop(path, None)


def make_storage(state: FakePgState, openbao: FakeOpenbao) -> PostgresStorage:
    storage = PostgresStorage(
        admin_url=ADMIN_URL, url_host=URL_HOST, url_port=URL_PORT, openbao=openbao
    )
    # monkeypatch 等价物：模块级 _connect 换替身（测试不连真库）
    storage_pg._connect = lambda url: FakePgConnection(state)  # type: ignore[assignment]
    return storage


@pytest.fixture(autouse=True)
def restore_connect():
    original = storage_pg._connect
    yield
    storage_pg._connect = original


def fresh_state(role_exists=False, db_exists=False) -> FakePgState:
    state = FakePgState()
    role = db = "wire_cell_acme"
    if role_exists:
        state.roles.add(role)
        state.passwords[role] = "preexistingpw"
    if db_exists:
        state.dbs.add(db)
    state.default_isolation(role, db)
    return state


class TestSevenStepProvision:
    def test_fresh_provision_full_flow(self):
        state = fresh_state()
        bao = FakeOpenbao()
        storage = make_storage(state, bao)

        url = storage.provision("acme")
        role = db = "wire_cell_acme"
        pw = state.passwords[role]

        # ⑦ URL 形态（docs §3.7）
        assert url == (
            f"postgresql+psycopg://{role}:{pw}@{URL_HOST}:{URL_PORT}/{db}"
        )
        assert HEX48_RE.match(pw), "token_hex(24) → 48 hex"
        # ② CREATE ROLE 口令走 %s 占位符（不拼进 SQL 文本）
        (create_sql, create_params), *_ = [
            x for x in state.executed if x[0].strip().upper().startswith("CREATE ROLE")
        ]
        assert pw not in create_sql and create_params == (pw,)
        # ③ CREATE DATABASE OWNER + 非超管管理员先 GRANT（PG16 CREATEROLE）
        assert state.sql("GRANT") and state.sql("CREATE DATABASE")
        assert any(
            "OWNER" in s and f'"{db}"' in s for s, _ in state.sql("CREATE DATABASE")
        )
        # ④ REVOKE PUBLIC CONNECT 无条件执行
        assert any("FROM PUBLIC" in s for s, _ in state.sql("REVOKE"))
        # ⑤ 四查真的发生了（4 条 has_database_privilege）
        assert len([1 for s, _ in state.executed if "has_database_privilege" in s]) == 4
        # ⑥ 条目入 OpenBAO：url/password/created_at，password 与 DDL 一致
        (path, entry), *_ = bao.put_calls
        assert path == "wire/cell-db/acme"
        assert entry["url"] == url and entry["password"] == pw
        assert re.match(r"^\d{4}-\d{2}-\d{2}T", entry["created_at"])
        # 足迹标记（回滚依据）
        assert storage._created["acme"] == {"role": True, "db": True, "secret": True}
        # 报告可观测（隔离四查真值）
        report = storage.report("acme")
        assert report["database"] == db
        assert report["isolation_checks"] == {
            "owner->cell_db": True,
            "owner->wire_platform": False,
            "probe_role": PROBE_ROLE,
            "unrelated->cell_db": False,
            "unrelated->wire_platform": False,
        }

    def test_superuser_admin_skips_grant(self):
        state = fresh_state()
        state.is_super = True
        storage = make_storage(state, FakeOpenbao())

        storage.provision("acme")

        assert state.sql("GRANT") == []
        assert state.sql("CREATE DATABASE")

    def test_psycopg_missing_gives_install_guidance(self, monkeypatch):
        # 不经 make_storage（那会替掉 _connect）：走真实 import 路径
        monkeypatch.setitem(sys.modules, "psycopg", None)  # import 即 ImportError
        storage = PostgresStorage(
            admin_url=ADMIN_URL, url_host=URL_HOST, url_port=URL_PORT, openbao=FakeOpenbao()
        )

        with pytest.raises(StorageProvisionError, match=r"pip install 'cell-manager\[postgres\]'"):
            storage.provision("acme")

    def test_invalid_identifier_rejected_before_any_sql(self):
        state = fresh_state()
        storage = make_storage(state, FakeOpenbao())

        with pytest.raises(StorageProvisionError, match=r"\^\\?\[a-z\]\[a-z0-9_\]"):
            storage.provision("Acme-Corp")
        with pytest.raises(StorageProvisionError, match="wire_cell_" + "x" * 31):
            storage.provision("x" * 31)
        assert state.executed == []


class TestIdempotentReentry:
    def test_role_db_entry_all_exist_reuses_password_without_ddl(self):
        state = fresh_state(role_exists=True, db_exists=True)
        bao = FakeOpenbao(entries={"wire/cell-db/acme": {"password": "oldpw", "url": "u"}})
        storage = make_storage(state, bao)

        url = storage.provision("acme")

        assert url.endswith(":oldpw@db-internal.cheap-3:5433/wire_cell_acme")
        assert "oldpw" in url
        # 复用不 ALTER、不 CREATE、不发新条目；REVOKE 仍幂等执行
        assert state.sql("ALTER ROLE") == []
        assert state.sql("CREATE ROLE") == []
        assert state.sql("CREATE DATABASE") == []
        assert bao.put_calls == []
        assert state.sql("REVOKE")
        # 无新建 → 回滚无动作
        assert storage._created["acme"] == {"role": False, "db": False, "secret": False}


class TestSelfHealRotation:
    def test_role_exists_entry_missing_rotates(self):
        state = fresh_state(role_exists=True, db_exists=True)
        bao = FakeOpenbao()
        storage = make_storage(state, bao)

        url = storage.provision("acme")
        new_pw = state.passwords["wire_cell_acme"]

        assert new_pw != "preexistingpw" and HEX48_RE.match(new_pw)
        # ALTER ROLE 新口令 + OpenBAO 落条目
        (alter_sql, alter_params), *_ = state.sql("ALTER ROLE")
        assert new_pw not in alter_sql and alter_params == (new_pw,)
        assert bao.put_calls and bao.entries["wire/cell-db/acme"]["password"] == new_pw
        assert new_pw in url
        # 角色非本次新建：回滚标记只记 secret（轮换后条目与口令自洽，保留）
        assert storage._created["acme"] == {"role": False, "db": False, "secret": True}

    def test_sealed_openbao_fails_closed_without_ddl_orphan(self):
        # 角色新建路径上 OpenBAO 封印：建角色/库已发生但凭据不入库 → 报错；
        # 足迹标记仍指向新建资产，供 manager 回滚清理
        state = fresh_state()
        bao = FakeOpenbao(fail_put=True)
        storage = make_storage(state, bao)

        with pytest.raises(OpenbaoError, match="解封"):
            storage.provision("acme")
        assert storage._created["acme"] == {"role": True, "db": True, "secret": False}


class TestIsolationFailure:
    def test_owner_lost_own_db_fails_closed_and_keeps_preexisting(self):
        # v1 手工形态（角色/库既有）四查不符：抛错，且不清既有资产
        state = fresh_state(role_exists=True, db_exists=True)
        state.privileges[("wire_cell_acme", "wire_cell_acme")] = False  # 假想 ACL 坏
        storage = make_storage(state, FakeOpenbao())

        with pytest.raises(StorageProvisionError, match="isolation check failed.*wire_cell_acme"):
            storage.provision("acme")

        storage.teardown_created("acme")
        assert "wire_cell_acme" in state.roles and "wire_cell_acme" in state.dbs

    def test_public_connect_leak_detected_via_probe(self):
        # REVOKE 失效形态：无关角色能连 cell 库 → fail-closed
        state = fresh_state()
        state.privileges[(PROBE_ROLE, "wire_cell_acme")] = True
        bao = FakeOpenbao()
        storage = make_storage(state, bao)

        with pytest.raises(StorageProvisionError, match="isolation check failed"):
            storage.provision("acme")
        # 新建资产在抛错后仍可经 teardown_created 回收
        storage.teardown_created("acme")
        assert "wire_cell_acme" not in state.roles
        assert "wire_cell_acme" not in state.dbs
        assert bao.entries == {}

    def test_password_redacted_from_wrapped_errors(self):
        # 中途任意 psycopg 异常：包装消息抹掉口令（服务端错误可能回显语句）

        class ExplodingOnRevoke(FakePgConnection):
            def execute(self, sql, params=None):
                if sql.strip().upper().startswith("REVOKE"):
                    pw = self.state.passwords["wire_cell_acme"]
                    raise RuntimeError(
                        f'server error LINE 1: CREATE ROLE "wire_cell_acme" PASSWORD {pw}'
                    )
                return FakePgConnection.execute(self, sql, params)

        state = fresh_state()
        storage = PostgresStorage(
            admin_url=ADMIN_URL, url_host=URL_HOST, url_port=URL_PORT, openbao=FakeOpenbao()
        )
        original_connect = storage_pg._connect
        storage_pg._connect = lambda url: ExplodingOnRevoke(state)  # type: ignore[assignment]
        try:
            with pytest.raises(StorageProvisionError) as exc_info:
                storage.provision("acme")
            assert state.passwords["wire_cell_acme"] not in str(exc_info.value)
            assert "***redacted***" in str(exc_info.value)
        finally:
            storage_pg._connect = original_connect


class TestTeardownCreated:
    def test_drops_only_what_this_run_created(self):
        state = FakePgState(roles={"wire_cell_other"}, dbs={"wire_cell_other"})
        state.default_isolation("wire_cell_acme", "wire_cell_acme")
        bao = FakeOpenbao()
        storage = make_storage(state, bao)

        storage.provision("acme")
        storage.teardown_created("acme")

        assert "wire_cell_acme" not in state.roles
        assert "wire_cell_acme" not in state.dbs
        assert bao.entries == {}  # 本次新建 → 托管条目一并清
        assert "wire_cell_other" in state.roles and "wire_cell_other" in state.dbs
        assert "acme" not in storage._created

    def test_no_markers_is_noop(self):
        state = fresh_state()
        storage = make_storage(state, FakeOpenbao())
        storage.teardown_created("acme")  # 未 provision 过：安静返回
        assert state.executed == []

    def test_rotation_path_keeps_preexisting_role(self):
        # 自愈轮换后回滚：角色既有 → 不删；条目保留（与 ALTER 后口令自洽）
        state = fresh_state(role_exists=True, db_exists=True)
        bao = FakeOpenbao()
        storage = make_storage(state, bao)
        storage.provision("acme")

        storage.teardown_created("acme")

        assert "wire_cell_acme" in state.roles and "wire_cell_acme" in state.dbs
        assert "wire/cell-db/acme" in bao.entries


class TestDestroy:
    def test_destroy_on_nothing_is_quiet(self):
        state = fresh_state()
        bao = FakeOpenbao()
        storage = make_storage(state, bao)

        storage.destroy("acme")  # 幂等：不存在即安静

        assert [s for s, _ in state.executed if "DROP" in s.upper()] == []
        assert "wire/cell-db/acme" in bao.delete_calls  # 条目清理仍执行（幂等）

    def test_destroy_drops_database_before_role(self):
        state = fresh_state(role_exists=True, db_exists=True)
        bao = FakeOpenbao(entries={"wire/cell-db/acme": {"password": "x"}})
        storage = make_storage(state, bao)

        storage.destroy("acme")

        drops = [s for s, _ in state.executed if s.strip().upper().startswith("DROP")]
        assert len(drops) == 2
        assert drops[0].upper().startswith("DROP DATABASE") and "FORCE" in drops[0].upper()
        assert drops[1].upper().startswith("DROP ROLE")
        assert "wire_cell_acme" not in state.roles and "wire_cell_acme" not in state.dbs
        assert bao.entries == {}

    def test_destroy_failure_raises_with_step(self):
        state = fresh_state(role_exists=True, db_exists=True)

        def connect(url):
            conn = FakePgConnection(state)
            original = conn.execute

            def execute(sql, params=None):
                if sql.strip().upper().startswith("DROP DATABASE"):
                    raise RuntimeError("db drop failed hard")
                return original(sql, params)

            conn.execute = execute  # type: ignore[method-assign]
            return conn

        storage_pg._connect = connect  # type: ignore[assignment]
        storage = PostgresStorage(
            admin_url=ADMIN_URL, url_host=URL_HOST, url_port=URL_PORT, openbao=FakeOpenbao()
        )
        with pytest.raises(StorageProvisionError, match="drop-database"):
            storage.destroy("acme")
