"""cell 数据根 PostgreSQL 建库面（wire-cell-pg-provisioning · 任务 1.x/2.x）。

把 v1 手工配方（docs/cell-postgres-storage.md §3）自动化：一 cell 一库
``wire_cell_<customer_id>`` + 同名属主登录角色 + ``REVOKE CONNECT … FROM
PUBLIC`` + 隔离四查 + 口令生成 + OpenBAO 凭据托管。provision 七步：

① psycopg 连管理库（延迟 import，缺失给 ``pip install 'cell-manager[postgres]'``
  指引）② 角色探测 pg_roles → 无则 CREATE ROLE LOGIN（随机 48-hex 口令）
③ 库探测 pg_database → 无则 CREATE DATABASE OWNER ④ REVOKE CONNECT … FROM
PUBLIC（无条件，幂等）⑤ 隔离四查（has_database_privilege；「无关登录角色」
取 admin 角色以外的任一非超管登录角色，取不到跳过该项并注明）⑥ OpenBAO
条目：有→复用口令；无且角色新建→put；无且角色已存在（v1 手工迁移形态）
→自愈轮换（新口令 + ALTER ROLE + put）⑦ 拼 ``postgresql+psycopg://`` URL
返回（即注入容器的 ``DAAS_DATABASE_URL``）。

红线（docs §2）：口令只在本模块局部变量与 OpenBAO 条目间流转——异常
消息 / teardown / destroy 路径一律不含口令（例外：provision 的返回值
URL 本身就是注入 env 的凭据值）。DDL 标识符一律双引号包裹前先过
``^[a-z][a-z0-9_]{0,39}$``（customer_id 已有 validate_customer_id，此处
对拼出的库名/角色名防御性再校验）；口令等值参数用 psycopg ``%s`` 占位符。
"""

from __future__ import annotations

import re
import secrets
from typing import Any

from cell_manager.audit import utc_now_iso
from cell_manager.errors import CellManagerError
from cell_manager.openbao import OpenbaoClient, OpenbaoError

#: cell 库 / 属主角色名前缀（与 v1 手工配方一致，docs §3.3）
CELL_DB_PREFIX = "wire_cell_"

#: 平台元数据库（隔离四查的「越库被拒」对象；baseline REVOKE 见 docs §3.2）
PLATFORM_DATABASE = "wire_platform"

#: cell 引擎 URL 驱动前缀（SQLAlchemy + psycopg3 白名单形态，docs §3.7）
URL_SCHEME = "postgresql+psycopg"

#: 口令随机字节（token_hex → 48 个十六进制字符；hex 字符集进 URL 无需转义）
PASSWORD_TOKEN_BYTES = 24

#: 拼接后的库名 / 角色名白名单（双引号标识符的防御性前置校验）
_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class StorageProvisionError(CellManagerError):
    """PG 建库七步之一失败（消息含步骤上下文与库名；**绝不带口令**）。

    继承 ``CellManagerError`` 使 Manager._run 的失败审计与 CLI 的基类捕获
    对存储故障一视同仁（形态上仍是 Exception 子类，契约不变）。
    """


def _connect(admin_url: str):  # pragma: no cover - 由集成腿与单测替身验证
    """连管理库（延迟 import；测试经 monkeypatch 本函数注入替身）。

    - psycopg 缺失 → ``StorageProvisionError``（带 ``pip install
      'cell-manager[postgres]'`` 指引），sqlite 画像零依赖不变。
    - SQLAlchemy 形态 ``postgresql+psycopg://`` 自动剥成 psycopg DSN。
    - ``ClientCursor``（client-side 参数绑定）：``%s`` 占位符可用于
      CREATE/ALTER ROLE 等工具语句——psycopg3 默认的 server-side 绑定对
      utility statement 报 ``syntax error at or near "$1"``（实测 PG16.15）。
    - autocommit：CREATE/DROP DATABASE 不能在事务块内执行。
    """
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - 环境差异分支，单测注入模拟
        raise StorageProvisionError(
            "psycopg 未安装：postgres 画像需要先执行 "
            "pip install 'cell-manager[postgres]'（sqlite 画像零依赖不变）"
        ) from exc
    dsn = admin_url
    if dsn.startswith("postgresql+psycopg://"):
        dsn = "postgresql://" + dsn[len("postgresql+psycopg://"):]
    return psycopg.connect(dsn, autocommit=True, cursor_factory=psycopg.ClientCursor)


def _redact(text: str, secrets_: tuple[str, ...]) -> str:
    """把口令等敏感值从文本中抹掉（服务端错误消息可能回显语句片段）。"""
    for value in secrets_:
        if value:
            text = text.replace(value, "***redacted***")
    return text


class PostgresStorage:
    """一 cell 一库的自动建库面（Manager 持有；sqlite 画像恒不构造）。"""

    def __init__(
        self,
        admin_url: str,
        url_host: str,
        url_port: str | int,
        openbao: OpenbaoClient,
        path_prefix: str = "wire/cell-db",
    ) -> None:
        self._admin_url = admin_url
        self._url_host = url_host
        self._url_port = str(url_port)
        self._openbao = openbao
        self._path_prefix = path_prefix.strip("/")
        #: provision 足迹标记（回滚只删本次新建者）：customer_id →
        #: {"role": bool, "db": bool, "secret": bool}
        self._created: dict[str, dict[str, bool]] = {}
        #: 隔离四查等验收输出（docs §7.5 试点锚点要「自动验证输出」可观测）
        self._reports: dict[str, dict[str, Any]] = {}

    # ---- 命名 ----

    @staticmethod
    def _identifier(customer_id: str) -> str:
        """库名 = 角色名 = ``wire_cell_<customer_id>``；白名单校验后才进 DDL。"""
        name = f"{CELL_DB_PREFIX}{customer_id}"
        if not _IDENTIFIER_RE.match(name):
            raise StorageProvisionError(
                f"invalid database/role name {name!r} derived from customer "
                f"{customer_id!r}: postgres storage requires the composed name to "
                r"match ^[a-z][a-z0-9_]{0,39}$ (lowercase alnum/underscore only, "
                f"customer_id <= 30 chars)"
            )
        return name

    def secret_path(self, customer_id: str) -> str:
        """OpenBAO 逻辑路径（policy wire-cell-db 只放行该前缀）。"""
        return f"{self._path_prefix}/{customer_id}"

    def report(self, customer_id: str) -> dict[str, Any]:
        """最近一次 provision 的验收输出（隔离四查真值 / 建了什么）。"""
        return self._reports.get(customer_id, {})

    # ---- 七步开通 ----

    def provision(self, customer_id: str) -> str:
        """建库 + 发凭据，返回注入 env 的 ``DAAS_DATABASE_URL``。

        口令只存在于局部变量 ``password`` 与 OpenBAO 条目中；任何异常
        路径的消息均经 ``_redact`` 抹敏。
        """
        db = self._identifier(customer_id)
        role = db
        path = self.secret_path(customer_id)
        markers = {"role": False, "db": False, "secret": False}
        self._created[customer_id] = markers
        password: str | None = None
        step = "connect"
        conn = None
        try:
            conn = _connect(self._admin_url)  # ①
            # ② 角色探测 → CREATE ROLE（LOGIN + 随机 48-hex 口令）
            step = "probe-role"
            role_exists = conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
            ).fetchone() is not None
            if not role_exists:
                step = "create-role"
                password = secrets.token_hex(PASSWORD_TOKEN_BYTES)
                conn.execute(f'CREATE ROLE "{role}" LOGIN PASSWORD %s', (password,))
                markers["role"] = True
            # ③ 库探测 → CREATE DATABASE OWNER（超管旁路；CREATEROLE 管理员
            # 需先 GRANT 新角色给自己才能建「属主=新角色」的库——PG16 不自动
            # 授 ADMIN（PG17 才改），实例 pg16-trixie 实测如此）
            step = "probe-database"
            db_exists = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (db,)
            ).fetchone() is not None
            if not db_exists:
                step = "create-database"
                admin_is_super = bool(
                    conn.execute(
                        "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
                    ).fetchone()[0]
                )
                if not admin_is_super:
                    conn.execute(f'GRANT "{role}" TO current_user')
                conn.execute(f'CREATE DATABASE "{db}" OWNER "{role}"')
                markers["db"] = True
            # ④ REVOKE PUBLIC CONNECT（无条件幂等；docs §3.5 机制解释）
            step = "revoke-public-connect"
            conn.execute(f'REVOKE CONNECT ON DATABASE "{db}" FROM PUBLIC')
            # ⑤ 隔离四查（has_database_privilege；不符即 fail-closed）
            step = "verify-isolation"
            checks = self._verify_isolation(conn, role, db)
            # ⑥ OpenBAO 凭据：有则复用；无则按角色新旧 put / 自愈轮换
            step = "openbao-credential"

            def _url(pw: str) -> str:
                return f"{URL_SCHEME}://{role}:{pw}@{self._url_host}:{self._url_port}/{db}"

            entry = self._openbao.get(path)
            if entry is not None:
                if not isinstance(entry.get("password"), str) or not entry["password"]:
                    raise StorageProvisionError(
                        f"openbao entry at secret/data/{path} is malformed "
                        "(missing non-empty 'password') — 现场核对条目后重试"
                    )
                password = entry["password"]
            elif markers["role"]:
                # 角色本次新建：CREATE ROLE 时的口令落库托管
                self._openbao.put(
                    path,
                    {"url": _url(password), "password": password,
                     "created_at": utc_now_iso()},
                )
                markers["secret"] = True
            else:
                # 角色已存在但条目缺失（v1 手工迁移形态）：自愈轮换
                password = secrets.token_hex(PASSWORD_TOKEN_BYTES)
                conn.execute(f'ALTER ROLE "{role}" PASSWORD %s', (password,))
                self._openbao.put(
                    path,
                    {"url": _url(password), "password": password,
                     "created_at": utc_now_iso()},
                )
                markers["secret"] = True
            # ⑦ 拼 URL 返回（唯一允许口令出现的位置——它就是注入 env 的凭据值）
            step = "compose-url"
            final_url = _url(password)
            self._reports[customer_id] = {
                "database": db,
                "role": role,
                "openbao_path": path,
                "created_role": markers["role"],
                "created_database": markers["db"],
                "isolation_checks": checks,
            }
            return final_url
        except (StorageProvisionError, OpenbaoError):
            raise  # 指引消息原样透传（已抹敏）
        except Exception as exc:
            raise StorageProvisionError(
                _redact(
                    f"postgres storage step {step!r} failed for database {db!r}: "
                    f"{type(exc).__name__}: {exc}",
                    (password or "",),
                )
            ) from exc
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001 —— 清理尽力
                    pass

    # ---- 隔离四查 ----

    def _verify_isolation(self, conn, role: str, db: str) -> dict[str, Any]:
        """四查（docs §3.4）：属主→本库 t / 属主→平台库 f /
        无关登录角色→cell 库 f / 无关登录角色→平台库 f。

        「无关登录角色」= admin 角色以外的任一**非超管**登录角色（超管旁路
        一切 ACL，恒 t，作探针必然误报）；取不到则跳过该项并在报告注明。
        """
        def _priv(grantee: str, target_db: str) -> bool:
            return bool(
                conn.execute(
                    "SELECT has_database_privilege(%s, %s, 'CONNECT')",
                    (grantee, target_db),
                ).fetchone()[0]
            )

        checks: dict[str, Any] = {
            "owner->cell_db": _priv(role, db),
            "owner->wire_platform": _priv(role, PLATFORM_DATABASE),
        }
        probe = conn.execute(
            "SELECT rolname FROM pg_roles WHERE rolcanlogin AND NOT rolsuper "
            "AND rolname <> current_user AND rolname <> %s ORDER BY rolname LIMIT 1",
            (role,),
        ).fetchone()
        if probe is None:
            note = "skipped: no unrelated non-superuser login role found on instance"
            checks["unrelated->cell_db"] = note
            checks["unrelated->wire_platform"] = note
        else:
            probe_role = probe[0]
            checks["probe_role"] = probe_role
            checks["unrelated->cell_db"] = _priv(probe_role, db)
            checks["unrelated->wire_platform"] = _priv(probe_role, PLATFORM_DATABASE)

        violations = []
        if checks["owner->cell_db"] is not True:
            violations.append(f"owner {role!r} cannot CONNECT own database {db!r}")
        if checks["owner->wire_platform"] is not False:
            violations.append(
                f"owner {role!r} may CONNECT platform database {PLATFORM_DATABASE!r}"
            )
        if checks["unrelated->cell_db"] is not False:
            violations.append(
                f"unrelated role {checks.get('probe_role', '?')!r} may CONNECT {db!r}"
            )
        if checks["unrelated->wire_platform"] is not False:
            violations.append(
                "unrelated role "
                f"{checks.get('probe_role', '?')!r} may CONNECT {PLATFORM_DATABASE!r}"
            )
        if violations:
            raise StorageProvisionError(
                f"isolation check failed for database {db!r}: "
                + "; ".join(violations)
                + " — 对照 docs/cell-postgres-storage.md §3.4/§3.5（REVOKE PUBLIC / baseline）"
            )
        return checks

    # ---- 回滚 / 销毁 ----

    def teardown_created(self, customer_id: str) -> None:
        """provision 失败回滚：只删**本次新建**的角色 / 库 / 托管条目。

        尽力而为不抛（调用方在异常回滚路径，原异常优先）。
        """
        markers = self._created.get(customer_id)
        if not markers or not any(markers.values()):
            return
        db = self._identifier(customer_id)
        conn = None
        try:
            conn = _connect(self._admin_url)
            if markers.get("db"):
                # 先删库再删角色（角色还挂着属主时 DROP ROLE 会被拒）
                conn.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
            if markers.get("role"):
                conn.execute(f'DROP ROLE IF EXISTS "{db}"')
            # 条目只在角色被本次新建时删除：角色已存在（自愈轮换路径）时
            # 条目与 ALTER 后的口令仍自洽，保留即一致。
            if markers.get("role") and markers.get("secret"):
                self._openbao.delete(self.secret_path(customer_id))
        except Exception:  # noqa: BLE001 —— 回滚尽力，原异常优先
            pass
        finally:
            self._created.pop(customer_id, None)
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass

    def destroy(self, customer_id: str) -> None:
        """purge 用：无条件幂等删库 + 删角色（先探测存在），失败抛出。

        同时删除 OpenBAO 托管条目——否则残留条目会让同客户重开时「复用
        旧口令」与新建角色的随机口令错配（自愈轮换只救条目缺失的形态）。
        """
        db = self._identifier(customer_id)
        step = "connect"
        conn = None
        try:
            conn = _connect(self._admin_url)
            step = "drop-database"
            if conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (db,)
            ).fetchone() is not None:
                conn.execute(f'DROP DATABASE "{db}" WITH (FORCE)')
            step = "drop-role"
            if conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (db,)
            ).fetchone() is not None:
                conn.execute(f'DROP ROLE "{db}"')
        except StorageProvisionError:
            raise
        except Exception as exc:
            raise StorageProvisionError(
                f"postgres storage destroy step {step!r} failed for "
                f"database {db!r}: {type(exc).__name__}: {exc}"
            ) from exc
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
        self._openbao.delete(self.secret_path(customer_id))  # 幂等（404 → 成功）
        self._created.pop(customer_id, None)
        self._reports.pop(customer_id, None)
