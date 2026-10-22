"""CellManager：企业 cell 生命周期状态机（规格 wire-cell-tenancy / 任务 5.1）。

状态机（cell = 容器 + 独立 SQLite WAL 卷 + registry 条目）。provision 步骤：
建卷 → 起容器 → **等就绪（wait_ready）** → 注册；创建之后的任何失败
原子回滚（删容器+删空卷），不留孤儿。::

    (不存在) --provision--> ACTIVE
    ACTIVE   --pause------> PAUSED       （卷保留）
    PAUSED   --resume-----> ACTIVE       （原卷重启）
    ACTIVE|PAUSED --deprovision--> RETENTION（purge_at = now + retention_days）
    RETENTION --cancel_deprovision--> ACTIVE（恢复，卷未动）
    RETENTION --到期 purge--> (不存在)   （删卷 + 摘 registry 条目，审计留档）

不变量：
- provision 幂等（ACTIVE 重入 no-op 返回现状）；其余状态重入即明确报错
- 每 cell 独立卷目录 / 独立容器名 / 独立 registry 条目路径（config 派生）
- 全部**变更**操作落 JSONL 审计（成功 ok / 失败 error 均留痕）；status 不留
"""

from __future__ import annotations

import math
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from cell_manager.audit import AuditLog, utc_now_iso
from cell_manager.config import CellManagerConfig, validate_customer_id
from cell_manager.db import init_volume
from cell_manager.drivers.base import BindMount, ContainerDriver, ContainerSpec
from cell_manager.errors import CellManagerError, InvalidCellStateError, UnknownCellError
from cell_manager.openbao import OpenbaoClient
from cell_manager.provenance import ProvenanceForwarder
from cell_manager.registry.base import RegistryClient, ServerEntry
from cell_manager.state import ACTIVE, PAUSED, RETENTION, StateStore
from cell_manager.storage_pg import PostgresStorage

#: 容器内数据卷挂载点（DAAS_DATABASE_URL 指向其下 daas.db）
CONTAINER_DATA_DIR = "/data"

NOT_FOUND = "not_found"


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class CellStatus:
    """status() 返回值（to_dict 供 CLI/HTTP 序列化）。"""

    customer_id: str
    state: str  # not_found | active | paused | retention
    container_name: str | None = None
    volume_path: str | None = None
    registry_path: str | None = None
    image: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    purge_at: str | None = None
    days_remaining: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "customer_id": self.customer_id,
            "state": self.state,
            "container_name": self.container_name,
            "volume_path": self.volume_path,
            "registry_path": self.registry_path,
            "image": self.image,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "purge_at": self.purge_at,
            "days_remaining": self.days_remaining,
        }


def _not_found(customer_id: str) -> CellStatus:
    return CellStatus(customer_id=customer_id, state=NOT_FOUND)


class CellManager:
    """生命周期管理面（纯依赖注入：driver/registry/clock 全可替换）。"""

    def __init__(
        self,
        *,
        driver: ContainerDriver,
        registry: RegistryClient,
        config: CellManagerConfig,
        clock: Callable[[], datetime] | None = None,
        audit_log: AuditLog | None = None,
        state_store: StateStore | None = None,
        provenance_forwarder: "ProvenanceForwarder | None" = None,
    ) -> None:
        self._driver = driver
        self._registry = registry
        self._config = config
        self._clock = clock or _default_clock
        self._audit = audit_log or AuditLog(config.audit_log)
        self._state = state_store or StateStore(config.state_file)
        #: 生命周期事件 → 平台溯源账本（wire-provenance-ledger 任务 4.2）。
        #: env 未配置 CELL_PROVENANCE_URL 时为 None（纯 JSONL 审计）。
        self._provenance = provenance_forwarder
        #: PG 数据根画像（wire-cell-pg-provisioning）：CELL_STORAGE=postgres 时
        #: 自动建库+发凭据（一 cell 一库，凭据入 OpenBAO）；sqlite 画像恒 None，
        #: 所有 postgres 分支零触达、行为与现状完全一致。
        self._storage: PostgresStorage | None = None
        if config.storage == "postgres":
            self._storage = PostgresStorage(
                admin_url=config.pg_admin_url,
                url_host=config.pg_url_host,
                url_port=config.pg_url_port,
                openbao=OpenbaoClient(config.openbao_addr, config.openbao_token),
                path_prefix=config.openbao_path_prefix,
            )

    # ==================== 生命周期操作（均落审计） ====================

    def provision(self, customer_id: str) -> CellStatus:
        """开通：建卷（空 WAL 库）→ 起容器（隔离 env）→ 注册 registry。

        幂等：已 ACTIVE 则 no-op 返回现状（不重复建容器/注册）；
        PAUSED / RETENTION 重入给明确指引错误。
        """

        def _do() -> tuple[CellStatus, dict[str, Any]]:
            validate_customer_id(customer_id)
            record = self._state.get(customer_id)
            if record is not None:
                state = record["state"]
                if state == ACTIVE:
                    return self.status(customer_id), {"idempotent": True}
                if state == PAUSED:
                    raise InvalidCellStateError(
                        f"cell for customer {customer_id!r} is paused; "
                        "run resume (not provision) to bring it back"
                    )
                raise InvalidCellStateError(
                    f"cell for customer {customer_id!r} is in retention "
                    f"({self._days_remaining(record)} days to purge); "
                    "run cancel-deprovision to restore it"
                )

            volume_dir = self._config.volume_dir(customer_id)
            container_name = self._config.container_name(customer_id)
            registry_path = self._config.registry_path(customer_id)
            db_path = init_volume(volume_dir)

            env = {
                # 隔离核心：每 cell 自己的库 + 同门计量入口（ADR-0002 / D5）。
                # SQLAlchemy 绝对路径四斜杠：sqlite:////data/daas.db
                "DAAS_DATABASE_URL": f"sqlite:///{CONTAINER_DATA_DIR}/daas.db",
                "WIRE_GATEWAY_URL": self._config.gateway_url,
                # cell 画像（daas-deployment-profiles / ADR-0001）：引擎只装载
                # 通用组+客户数据集组。缺了它 default 画像没有 customer_dataset
                # 组，数据集同步全部 502——cell 形态的硬前置。
                "DAAS_PROFILE": "cell",
            }
            # 双轨凭据（8.3 G4）：客户轨 per-cell（CELL_CUSTOMER_CREDENTIAL_MAP）、
            # 平台轨全局单值（CELL_PLATFORM_CREDENTIAL）。未配置则不注入——
            # cell 内 wire_auth_proxy 对缺 key 的轨道 fail-closed（503），
            # 生产必须配置（见 README「配置」节与 docs/service-credentials.md）。
            customer_credential = self._config.customer_credential_map.get(customer_id)
            if customer_credential:
                env["WIRE_CUSTOMER_KEY"] = customer_credential
            if self._config.platform_credential:
                env["WIRE_PLATFORM_KEY"] = self._config.platform_credential
            # 指标拉取通道（wire-customer-data-engine §4/7.4）：join_indicators
            # 经 wire 入口读平台指标——入口=registry 的共享业务面条目（计量轨，
            # 与产物物化同构），凭据=本 cell 的客户 wgk key。env 缺失时引擎
            # join 工具如实报「通道未配置」（fail-closed，不降级本地拉数）。
            if customer_credential:
                env["FD_CELL_WGK_KEY"] = customer_credential
                env["FD_WIRE_ENTRANCE_URL"] = (
                    f"{self._config.registry_url.rstrip('/')}"
                    "/fd-find-data-business-mcp/mcp"
                )
            # 存储配额（wire-cell-storage-quota）：注入引擎 env，PG 根下超限
            # 整批拒绝；sqlite 根不生效。两画像注入行为一致（引擎侧裁决），
            # 变更配额需重建容器（env 语义）。
            if self._config.pg_quota_mb:
                env["FD_CELL_STORAGE_QUOTA_MB"] = str(self._config.pg_quota_mb)

            # 创建→启动→等就绪→注册，整体原子：任何一步失败即回滚
            # （删容器+删刚建的空卷+删本次新建的 PG 角色/库），不留孤儿。
            # 等就绪是注册的硬前置：registry 注册即触发安全扫描与健康探针，
            # 打未就绪 cell 会被判不可达并自动禁用（2026-10-07 生产实证）。
            # PG 画像的建库放在同一原子圈内：DAAS_DATABASE_URL 从 sqlite 缺省
            # 覆盖为 wire_cell_<id> 的 PostgreSQL URL（wire-cell-pg-provisioning）。
            try:
                if self._storage is not None:
                    env["DAAS_DATABASE_URL"] = self._storage.provision(customer_id)
                spec = ContainerSpec(
                    name=container_name,
                    image=self._config.image,
                    env=env,
                    network=self._config.network,
                    binds=(BindMount(host_path=str(db_path.parent), container_path=CONTAINER_DATA_DIR),),
                )
                self._driver.create(spec)
                self._driver.start(container_name)
                self._driver.wait_ready(
                    container_name, timeout=self._config.readiness_timeout_seconds
                )
                self._registry.register(
                    ServerEntry(
                        path=registry_path,
                        target_url=self._config.target_url(customer_id),
                        display_name=f"柏讯 cell · {customer_id}",
                        # cell 授权束·发现面（group-restricted + 本客户合成组）；
                        # 调用面半边由 grant 铸 scope 文档（ADR 0005）。
                        visibility="group-restricted",
                        allowed_groups=(self._config.cell_group(customer_id),),
                    )
                )
            except Exception:
                try:
                    if self._driver.exists(container_name):
                        self._driver.remove(container_name, force=True)
                except Exception:  # noqa: BLE001 —— 回滚尽力，原异常优先
                    pass
                # PG 画像：只删本次 provision 新建的角色/库（尽力而为，
                # 原异常优先；半建的既有资产——如 v1 手工角色——不动）
                if self._storage is not None:
                    try:
                        self._storage.teardown_created(customer_id)
                    except Exception:  # noqa: BLE001 —— 回滚尽力，原异常优先
                        pass
                shutil.rmtree(volume_dir, ignore_errors=True)
                raise
            now = utc_now_iso()
            record = {
                "customer_id": customer_id,
                "state": ACTIVE,
                "container_name": container_name,
                "volume_path": str(volume_dir),
                "registry_path": registry_path,
                "image": self._config.image,
                # 数据根画像（sqlite 旧记录读侧用 record.get("storage","sqlite")）
                "storage": self._config.storage,
                "created_at": now,
                "updated_at": now,
                "purge_at": None,
            }
            self._state.put(record)
            detail = {
                "db_path": str(db_path),
                "target_url": self._config.target_url(customer_id),
                # 凭据注入留痕（只记 env 名，不落凭据明文）
                "injected_credential_env": [
                    key
                    for key in ("WIRE_CUSTOMER_KEY", "WIRE_PLATFORM_KEY")
                    if key in spec.env
                ],
            }
            if self._storage is not None:
                # PG 画像留痕：库名 / 托管路径 / 注入的凭据 env 名——
                # 只有名字，绝无口令与 URL 明文
                detail.update(
                    storage="postgres",
                    database=f"wire_cell_{customer_id}",
                    openbao_path=self._storage.secret_path(customer_id),
                )
                detail["injected_credential_env"].append("DAAS_DATABASE_URL")
                # 隔离四查的自动验证输出（docs §7.5 试点锚点要可观测；
                # 布尔真值与 skip 注记，无任何凭据）
                report = self._storage.report(customer_id)
                if report.get("isolation_checks"):
                    detail["storage_isolation"] = report["isolation_checks"]
            return self._record_status(record), detail

        return self._run("provision", customer_id, _do)

    def pause(self, customer_id: str) -> CellStatus:
        """暂停：停容器，数据卷保留（欠费/客户请求）。"""

        def _do() -> tuple[CellStatus, dict[str, Any]]:
            record = self._require(customer_id, expect=ACTIVE, action="pause")
            self._driver.stop(record["container_name"])
            return self._transition(record, PAUSED), {}

        return self._run("pause", customer_id, _do)

    def resume(self, customer_id: str) -> CellStatus:
        """恢复：原卷重启容器。"""

        def _do() -> tuple[CellStatus, dict[str, Any]]:
            record = self._require(customer_id, expect=PAUSED, action="resume")
            self._driver.start(record["container_name"])
            self._driver.wait_ready(
                record["container_name"], timeout=self._config.readiness_timeout_seconds
            )
            return self._transition(record, ACTIVE), {}

        return self._run("resume", customer_id, _do)

    def deprovision(self, customer_id: str) -> CellStatus:
        """申请注销：停容器（若在跑），进入保留期（CELL_RETENTION_DAYS，默认 30 天）。"""

        def _do() -> tuple[CellStatus, dict[str, Any]]:
            record = self._require(customer_id, expect=(ACTIVE, PAUSED), action="deprovision")
            if record["state"] == ACTIVE:
                self._driver.stop(record["container_name"])
            purge_at = self._clock() + timedelta(days=self._config.retention_days)
            status = self._transition(record, RETENTION, purge_at=purge_at)
            return status, {"retention_days": self._config.retention_days}

        return self._run("deprovision", customer_id, _do)

    def cancel_deprovision(self, customer_id: str) -> CellStatus:
        """保留期内反悔：原卷重启容器，恢复 ACTIVE。"""

        def _do() -> tuple[CellStatus, dict[str, Any]]:
            record = self._require(customer_id, expect=RETENTION, action="cancel_deprovision")
            self._driver.start(record["container_name"])
            self._driver.wait_ready(
                record["container_name"], timeout=self._config.readiness_timeout_seconds
            )
            return self._transition(record, ACTIVE, purge_at=None), {}

        return self._run("cancel_deprovision", customer_id, _do)

    def purge(self, customer_id: str) -> CellStatus:
        """清除（不可逆）：删容器 + 删卷 +（PG 画像）删 wire_cell_<id> 库/角色 +
        摘 registry 条目 + 删授权 scope + 删簿记；审计留档。"""

        def _do() -> tuple[CellStatus, dict[str, Any]]:
            record = self._require(customer_id, expect=RETENTION, action="purge")
            container_name = record["container_name"]
            volume_path = Path(record["volume_path"])
            if self._driver.exists(container_name):
                self._driver.remove(container_name, force=True)
            if volume_path.exists():
                shutil.rmtree(volume_path)
            # PG 画像：与删卷同批清 wire_cell_<id> 库+角色（幂等、失败抛出，
            # 不静默半删——容器/卷已清、库角色残留时 purge 可整段重跑）。
            # deprovision 不动库（保留期语义，见方法 docstring 与 docs §7.3）。
            if record.get("storage") == "postgres" and self._storage is not None:
                self._storage.destroy(customer_id)
            unregistered = self._registry.unregister(record["registry_path"])
            # 授权束回收兜底（正常路径 deprovision 阶段已删；此处幂等再删一次，
            # 防中途 cancel 后直奔 purge 的绕行）。
            # 通道与 CellGrant.teardown_registry_face 对齐（R5）：旧
            # DELETE /api/iam/groups 被边缘拦 405，必须走 internal delete-group；
            # 删不掉不中止其余清理（残留可见可补 > 卡死半途），但如实留痕。
            scope_deleted, scope_error = self._delete_scope_resilient(customer_id)
            self._state.delete(customer_id)
            return _not_found(customer_id), {
                "volume_removed": str(volume_path),
                "registry_unregistered": unregistered,
                "scope_deleted": scope_deleted,
                "scope_error": scope_error,
                "irreversible": True,
            }

        return self._run("purge", customer_id, _do)

    def _delete_scope_resilient(self, customer_id: str) -> tuple[bool, str | None]:
        """删授权 scope（R5 通道优先，失败不抛）：返回 (deleted, error)。

        - ``CELL_REGISTRY_SECRET_KEY_FILE`` 已配 → ``InternalScopeDeleter``
          （POST /api/internal/delete-group + delete_from_keycloak=false，
          合成组轨道完全不碰 IdP；旧 /api/iam/groups 路径被边缘拦 405）。
        - 未配 → 回落既有 registry 客户端（保留旧部署可用的兼容面）。
        """
        scope_name = self._config.scope_name(customer_id)
        secret_key_file = getattr(self._config, "registry_secret_key_file", "")
        if secret_key_file:
            try:
                from cell_manager.keys import InternalScopeDeleter, load_secret_key

                deleter = InternalScopeDeleter(
                    self._config.registry_url, load_secret_key(secret_key_file)
                )
                return deleter.delete(scope_name), None
            except Exception as exc:  # noqa: BLE001 —— 残留可见优先于中止
                return False, f"{type(exc).__name__}: {exc}"
        try:
            return self._registry.delete_scope(scope_name), None
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    def purge_expired(self) -> list[CellStatus]:
        """批量清除所有到期 retention cell（后台定时任务入口）。"""
        purged: list[CellStatus] = []
        now = self._clock()
        for customer_id, record in sorted(self._state.all_cells().items()):
            if record["state"] != RETENTION:
                continue
            if self._parse_ts(record["purge_at"]) <= now:
                purged.append(self.purge(customer_id))
        return purged

    # ==================== 查询（不留审计） ====================

    def status(self, customer_id: str) -> CellStatus:
        """状态查询：not_found / active / paused / retention(+剩余天数)。"""
        validate_customer_id(customer_id)
        record = self._state.get(customer_id)
        if record is None:
            return _not_found(customer_id)
        return self._record_status(record)

    # ==================== 内部 ====================

    def _run(
        self,
        action: str,
        customer_id: str,
        fn: Callable[[], tuple[CellStatus, dict[str, Any]]],
    ) -> CellStatus:
        """执行变更操作并落审计：成功 ok+detail，失败 error+message（均追加 JSONL）。"""
        try:
            status, detail = fn()
        except CellManagerError as exc:
            self._audit.append(
                action=action,
                customer_id=str(customer_id),
                result="error",
                detail={"type": type(exc).__name__, "message": str(exc)},
            )
            raise
        self._audit.append(action=action, customer_id=str(customer_id), result="ok", detail=detail or None)
        if self._provenance is not None:
            # 成功 op 才入账（失败已留 JSONL）；转发 best-effort，绝不打断生命周期。
            self._provenance.emit(action, str(customer_id), detail)
        return status

    def _record_status(self, record: dict[str, Any]) -> CellStatus:
        days_remaining = None
        if record["state"] == RETENTION:
            days_remaining = self._days_remaining(record)
        return CellStatus(
            customer_id=record["customer_id"],
            state=record["state"],
            container_name=record["container_name"],
            volume_path=record["volume_path"],
            registry_path=record["registry_path"],
            image=record["image"],
            created_at=record["created_at"],
            updated_at=record["updated_at"],
            purge_at=record.get("purge_at"),
            days_remaining=days_remaining,
        )

    def _days_remaining(self, record: dict[str, Any]) -> int:
        remaining = self._parse_ts(record["purge_at"]) - self._clock()
        return max(0, math.ceil(remaining.total_seconds() / 86400))

    def _transition(
        self,
        record: dict[str, Any],
        state: str,
        *,
        purge_at: datetime | None = None,
    ) -> CellStatus:
        record = dict(record)
        record["state"] = state
        record["updated_at"] = utc_now_iso()
        record["purge_at"] = purge_at.isoformat(timespec="seconds") if purge_at else None
        self._state.put(record)
        return self._record_status(record)

    def _require(self, customer_id: str, *, expect: str | tuple[str, ...], action: str) -> dict[str, Any]:
        validate_customer_id(customer_id)
        record = self._state.get(customer_id)
        if record is None:
            raise UnknownCellError(
                f"no cell for customer {customer_id!r} (never provisioned or already purged)"
            )
        expected = (expect,) if isinstance(expect, str) else expect
        if record["state"] not in expected:
            raise InvalidCellStateError(
                f"cannot {action} cell for customer {customer_id!r}: "
                f"current state is {record['state']!r}, expected {' or '.join(expected)!r}"
            )
        return record

    @staticmethod
    def _parse_ts(value: str | None) -> datetime:
        return datetime.fromisoformat(value) if value else datetime.max.replace(tzinfo=timezone.utc)
