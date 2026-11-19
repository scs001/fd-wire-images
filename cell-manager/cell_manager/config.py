"""cell-manager 配置：全部 env 可配（README「借用契约」同款纪律）。

布局（单一 home 之下，卷与簿记分离）::

    <home>/volumes/<customer_id>/daas.db   # 每 cell 独立数据卷（平台侧永不直读）
    <home>/state/cells.json                # 生命周期簿记（原子写）
    <home>/audit/audit.log                 # JSONL 审计留档（append-only）

环境变量::

    CELL_MANAGER_HOME      管理面根目录（默认 ~/.fd-wire/cell-manager）
    CELL_DATA_ROOT         数据卷根（默认 <home>/volumes）
    CELL_STATE_FILE        簿记文件（默认 <home>/state/cells.json）
    CELL_AUDIT_LOG         审计日志（默认 <home>/audit/audit.log）
    CELL_IMAGE             cell 容器镜像（占位默认，部署时指 TCR 真实镜像）
    CELL_GATEWAY_URL       注入容器的 WIRE_GATEWAY_URL（wire 入口，计量门）
    CELL_REGISTRY_URL      registry 借用契约地址（注册 server 条目用）
    CELL_REGISTRY_MODE     registry client 形态：http | fake（默认 http）
    CELL_REGISTRY_AUTH/TOKEN/COOKIE/CSRF  http 模式鉴权（见 registry/http.py::auth_from_env）
    CELL_TARGET_URL_TEMPLATE  registry 条目 target 模板（{container_name} 占位）
    CELL_RETENTION_DAYS    注销保留期天数（默认 30，0=立即到期）
    CELL_DRIVER            容器驱动：docker | fake（默认 docker）
    CELL_CUSTOMER_CREDENTIAL_MAP  客户轨凭据 JSON（customer_id → key），
                                  provision 时注入容器 WIRE_CUSTOMER_KEY；
                                  未配置则不注入（cell 内代理客户轨 fail-closed，
                                  生产必须配置——见 README）
    CELL_CUSTOMER_IDENTITY_MAP    开户身份 JSON（customer_id →
                                  {"username": ..., "sub2api_user_id": ...}），
                                  cell 授权束（grant/mint-key/退租）的输入；
                                  username=registry/Logto 侧身份（铸 key 的 sub），
                                  sub2api_user_id=wire api 建户时已持有的账户标识。
                                  未配置则 grant/mint-key 明确报错（不静默降级）
    CELL_REGISTRY_SECRET_KEY_FILE registry 根信任材料位置（mint-key 用）：
                                  指向 registry .env（含 SECRET_KEY= 行，自动摘取）
                                  或裸 secret 文件（0600）。仅在 the node 本机配置；
                                  未配置则 mint-key 不可用（铸 key 需手工通道）
    CELL_PREFLIGHT_MAP_PATH       registry 预检映射写入 API 路径（切片二 3.1 交付），
                                  如 /api/sub2api-caller-map；未配置则 grant 跳过
                                  该步并在审计留痕（切片一验收走 env 覆盖层）
    CELL_PLATFORM_CREDENTIAL     平台轨凭据单值（全部 cell 共用），
                                  provision 时注入容器 WIRE_PLATFORM_KEY；
                                  未配置则不注入（同上，生产必须配置）
    CELL_STORAGE                 cell 数据根画像：sqlite | postgres（默认
                                  sqlite=行为零变化）。postgres=一 cell 一库，
                                  自动建库+发凭据（wire-cell-pg-provisioning）
    CELL_PG_ADMIN_URL            postgres 画像管理连接（SQLAlchemy 形态
                                  postgresql+psycopg://…；仅 DDL/探测用，
                                  永不读业务数据——红线见
                                  docs/cell-postgres-storage.md §2）
    CELL_PG_URL_HOST/PORT        cell 容器视角的 PG 地址（拼
                                  DAAS_DATABASE_URL 用，与管理地址可以不同）
    CELL_SECRETS_DIR             凭据托管目录（默认 <home>/secrets；一 cell 一
                                  文件 0600，remove-cell-openbao-dependency：
                                  OpenBAO 退役后凭据落本地文件轨道）

    storage=postgres 时 CELL_PG_ADMIN_URL / CELL_PG_URL_HOST / CELL_PG_URL_PORT
    **全部必需**（fail-closed，缺哪个点名哪个）；另需 ``pip install
    'cell-manager[postgres]'``（psycopg）。CELL_OPENBAO_* 系列已废弃：读取到时
    仅记一条告警日志，不再参与构造与校验（sqlite 画像与 postgres 画像同此口径）。
"""

from __future__ import annotations

import json
import logging
import re
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .errors import ConfigError, InvalidCustomerIdError

#: customer_id 白名单：字母数字开头，仅 [A-Za-z0-9._-]，长度 1..64。
#: 首字符必须字母数字，天然排除 "." / ".." / 绝对路径等路径逃逸形态。
_CUSTOMER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: registry server 条目路径与容器名共用前缀（wire 入口同门计量，任务 5.2 对齐）
CELL_NAME_PREFIX = "fd-wire-cell-"

#: cell 授权束命名约定（add-customer-onboarding-automation / ADR 0005）：
#: 合成组只活在 key 组快照与 scope 文档 group_mappings，不经 Logto。
WIRE_CUSTOMERS_GROUP = "wire-customers"
CELL_GROUP_PREFIX = "wire-cust-"
CELL_SCOPE_PREFIX = "mcp-wire-cell-"

DEFAULT_IMAGE = "registry.tencentyun.com/finddata/fd-daas-mcp:placeholder"
DEFAULT_GATEWAY_URL = "https://wire.finddatatech.cloud/mcp"
DEFAULT_REGISTRY_URL = "https://mcp.finddatatech.cloud"
DEFAULT_TARGET_URL_TEMPLATE = "http://{container_name}:8000/mcp"
DEFAULT_RETENTION_DAYS = 30


def validate_customer_id(customer_id: str) -> str:
    """校验并原样返回 customer_id；不合法即抛 ``InvalidCustomerIdError``。"""
    if not isinstance(customer_id, str) or not _CUSTOMER_ID_RE.match(customer_id):
        raise InvalidCustomerIdError(
            f"invalid customer_id {customer_id!r}: must match "
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,63} (alnum start, no path separators)"
        )
    return customer_id


def _parse_customer_credential_map(raw: str) -> dict[str, str]:
    """解析 ``CELL_CUSTOMER_CREDENTIAL_MAP``（JSON 对象：customer_id → key）。

    空/未配置 → 空映射（provision 不注入 ``WIRE_CUSTOMER_KEY``，cell 内
    代理客户轨 fail-closed——生产必须配置）。键须是合法 customer_id，
    值须是非空字符串；非法即 ``ConfigError``（启动期 fail-fast）。
    """
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"CELL_CUSTOMER_CREDENTIAL_MAP must be a JSON object (customer_id -> key): {exc}"
        ) from exc
    if not isinstance(parsed, dict) or not all(
        isinstance(key, str) and isinstance(value, str) and value.strip()
        for key, value in parsed.items()
    ):
        raise ConfigError(
            "CELL_CUSTOMER_CREDENTIAL_MAP must be a JSON object of "
            "customer_id -> non-empty credential string"
        )
    for key in parsed:
        validate_customer_id(key)
    return dict(parsed)


@dataclass(frozen=True)
class CustomerIdentity:
    """开户身份（``CELL_CUSTOMER_IDENTITY_MAP`` 的值）：cell 授权束的输入。

    username = registry/Logto 侧身份（铸客户 key 的 sub、预检映射的键）；
    sub2api_user_id = wire api 建 sub2api 账户时已持有的账户标识（预检映射的值，
    不做二次查询——设计 D3）。
    """

    username: str
    sub2api_user_id: str


def _parse_customer_identity_map(raw: str) -> dict[str, CustomerIdentity]:
    """解析 ``CELL_CUSTOMER_IDENTITY_MAP``（JSON：customer_id → 身份对象）。

    空/未配置 → 空映射（grant/mint-key 对缺失客户明确报错，不静默降级）。
    值形状 ``{"username": 非空, "sub2api_user_id": 非空}``；多余键忽略
    （前向兼容：身份对象将来可扩展，如连接器信息）。
    """
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"CELL_CUSTOMER_IDENTITY_MAP must be a JSON object (customer_id -> identity): {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ConfigError(
            "CELL_CUSTOMER_IDENTITY_MAP must be a JSON object of "
            "customer_id -> {username, sub2api_user_id}"
        )
    mapping: dict[str, CustomerIdentity] = {}
    for key, value in parsed.items():
        validate_customer_id(key)
        if not isinstance(value, dict):
            raise ConfigError(
                f"CELL_CUSTOMER_IDENTITY_MAP[{key!r}] must be an object "
                "{username, sub2api_user_id}"
            )
        username = str(value.get("username") or "").strip()
        user_id = str(value.get("sub2api_user_id") or "").strip()
        if not username or not user_id:
            raise ConfigError(
                f"CELL_CUSTOMER_IDENTITY_MAP[{key!r}] needs non-empty "
                f"username and sub2api_user_id"
            )
        mapping[key] = CustomerIdentity(username=username, sub2api_user_id=user_id)
    return mapping


@dataclass(frozen=True)
class CellManagerConfig:
    """运行时配置（不可变；``from_env`` 构造，测试可直构或注入 env mapping）。"""

    home: Path
    data_root: Path
    state_file: Path
    audit_log: Path
    image: str
    gateway_url: str
    registry_url: str
    registry_mode: str
    target_url_template: str
    retention_days: int
    driver_name: str
    #: customer_id → 客户轨凭据（CELL_CUSTOMER_CREDENTIAL_MAP 解析；
    #: 空映射=不注入 WIRE_CUSTOMER_KEY，cell 内代理客户轨 fail-closed）
    customer_credential_map: Mapping[str, str] = field(default_factory=dict)
    #: customer_id → 开户身份（CELL_CUSTOMER_IDENTITY_MAP 解析；授权束输入）
    customer_identity_map: Mapping[str, CustomerIdentity] = field(default_factory=dict)
    #: registry 根信任材料文件（CELL_REGISTRY_SECRET_KEY_FILE；""=mint-key 不可用）
    registry_secret_key_file: str = ""
    #: registry 预检映射写入 API 路径（CELL_PREFLIGHT_MAP_PATH；""=grant 跳过该步）
    preflight_map_path: str = ""
    #: 平台轨凭据单值（CELL_PLATFORM_CREDENTIAL；空=不注入 WIRE_PLATFORM_KEY）
    platform_credential: str = ""
    #: docker 网络名（CELL_NETWORK；None=驱动缺省网络）。wire cell 必须与
    #: registry 栈同网，注册的 target_url（容器名:端口）才能被网关解析。
    network: str | None = None
    #: 就绪等待超时秒数（CELL_READINESS_TIMEOUT）。start→等健康→register 的
    #: 窗口：注册即触发安全扫描与健康探针，打未就绪 cell 会被判不可达并
    #: 自动禁用（2026-10-07 生产实证）；cell 镜像 HEALTHCHECK start-period 25s。
    readiness_timeout_seconds: float = 120.0
    #: cell 数据根画像（CELL_STORAGE：sqlite|postgres；sqlite=行为零变化）
    storage: str = "sqlite"
    #: postgres 画像管理连接（CELL_PG_ADMIN_URL；仅 DDL/探测，永不读业务数据）
    pg_admin_url: str = ""
    #: cell 容器视角的 PG 地址（CELL_PG_URL_HOST / CELL_PG_URL_PORT）
    pg_url_host: str = ""
    pg_url_port: str = ""
    #: 凭据托管目录（CELL_SECRETS_DIR；None=缺省 <home>/secrets。一 cell 一
    #: 文件 0600，remove-cell-openbao-dependency：
    #: OpenBAO 退役后凭据落本地文件轨道）
    secrets_dir: Path | None = None
    #: cell 存储配额 MB（CELL_PG_QUOTA_MB；可选正整数。注入容器 env
    #: FD_CELL_STORAGE_QUOTA_MB，引擎 PG 根侧整批拒绝超限写入；sqlite 根
    #: 不生效。变更配额需重建容器——env 语义，文档见
    #: docs/cell-postgres-storage.md「存储配额」）
    pg_quota_mb: int | None = None
    #: fd-wire 配给队列入口（FD_WIRE_INTERNAL_URL；runner 子命令用，
    #: 形如 https://wire-api.internal）。空 = runner 不可用（fail-closed）。
    fd_wire_internal_url: str = ""
    #: fd-wire 内部令牌（FD_WIRE_INTERNAL_TOKEN；X-Internal-Token 头）。
    fd_wire_internal_token: str = ""
    #: relay 对外地址（FD_WIRE_RELAY_HOST；尾网地址如 <relay-host>——registry
    #: 条目 proxy_pass_url 必须是集群可达地址，docker 网络名会被渲染成注释块，
    #: 2026-10-24 handoff §三实证）。空 = relay 步不可用（fail-closed）。
    relay_host: str = ""
    #: relay 监听端口基（FD_WIRE_RELAY_PORT_BASE；每客户端口=基 + crc32(cid)
    #: % 2000 的稳定派生，冲突重跑自愈靠 socat reuseaddr+幂等重注册）。
    relay_port_base: int = 18005
    #: runner 轮询间隔秒（FD_WIRE_RUNNER_INTERVAL；常驻模式 sleep 步长）。
    runner_poll_seconds: int = 300

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "CellManagerConfig":
        """从环境（或注入的 mapping，测试用）构造；非法取值抛 ``ConfigError``。"""
        e = dict(os.environ if env is None else env)
        home = Path(e.get("CELL_MANAGER_HOME") or Path.home() / ".fd-wire" / "cell-manager")
        data_root = Path(e.get("CELL_DATA_ROOT") or home / "volumes")
        state_file = Path(e.get("CELL_STATE_FILE") or home / "state" / "cells.json")
        audit_log = Path(e.get("CELL_AUDIT_LOG") or home / "audit" / "audit.log")
        raw_days = e.get("CELL_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS))
        try:
            retention_days = int(raw_days)
        except ValueError as exc:
            raise ConfigError(f"CELL_RETENTION_DAYS must be an integer, got {raw_days!r}") from exc
        if retention_days < 0:
            raise ConfigError(f"CELL_RETENTION_DAYS must be >= 0, got {retention_days}")
        driver_name = e.get("CELL_DRIVER", "docker").strip().lower()
        if driver_name not in ("docker", "fake"):
            raise ConfigError(f"CELL_DRIVER must be 'docker' or 'fake', got {driver_name!r}")
        registry_mode = e.get("CELL_REGISTRY_MODE", "http").strip().lower()
        if registry_mode not in ("http", "fake"):
            raise ConfigError(f"CELL_REGISTRY_MODE must be 'http' or 'fake', got {registry_mode!r}")
        customer_credential_map = _parse_customer_credential_map(
            e.get("CELL_CUSTOMER_CREDENTIAL_MAP", "")
        )
        customer_identity_map = _parse_customer_identity_map(
            e.get("CELL_CUSTOMER_IDENTITY_MAP", "")
        )
        platform_credential = e.get("CELL_PLATFORM_CREDENTIAL", "").strip()
        raw_readiness = e.get("CELL_READINESS_TIMEOUT", "120")
        try:
            readiness_timeout = float(raw_readiness)
        except ValueError as exc:
            raise ConfigError(
                f"CELL_READINESS_TIMEOUT must be a number, got {raw_readiness!r}"
            ) from exc
        if readiness_timeout <= 0:
            raise ConfigError(
                f"CELL_READINESS_TIMEOUT must be > 0, got {readiness_timeout}"
            )
        # cell 数据根画像（wire-cell-pg-provisioning）：sqlite=零变化；
        # postgres=一 cell 一库 + secrets 文件凭据托管，三项 PG env
        # fail-closed 必需（OpenBAO 依赖已随 remove-cell-openbao-dependency 移除）。
        storage = e.get("CELL_STORAGE", "sqlite").strip().lower()
        if storage not in ("sqlite", "postgres"):
            raise ConfigError(
                f"CELL_STORAGE must be 'sqlite' or 'postgres', got {storage!r}"
            )
        pg_admin_url = e.get("CELL_PG_ADMIN_URL", "").strip()
        pg_url_host = e.get("CELL_PG_URL_HOST", "").strip()
        pg_url_port = e.get("CELL_PG_URL_PORT", "").strip()
        secrets_dir_raw = e.get("CELL_SECRETS_DIR", "").strip()
        secrets_dir = Path(secrets_dir_raw) if secrets_dir_raw else home / "secrets"
        raw_quota = e.get("CELL_PG_QUOTA_MB", "").strip()
        pg_quota_mb: int | None = None
        if raw_quota:
            try:
                pg_quota_mb = int(raw_quota)
            except ValueError as exc:
                raise ConfigError(
                    f"CELL_PG_QUOTA_MB must be a positive integer, got {raw_quota!r}"
                ) from exc
            if pg_quota_mb <= 0:
                raise ConfigError(
                    f"CELL_PG_QUOTA_MB must be a positive integer, got {raw_quota!r}"
                )
        if pg_url_port and not pg_url_port.isdigit():
            raise ConfigError(
                f"CELL_PG_URL_PORT must be a port number, got {pg_url_port!r}"
            )
        # runner（add-self-serve-cell-provision 2.4）：fd-wire 队列入口与
        # relay 画像。缺省为空 = runner 子命令不可用（运行期 fail-closed
        # 点名缺哪个）；普通生命周期动作完全不受影响。
        fd_wire_internal_url = e.get("FD_WIRE_INTERNAL_URL", "").strip().rstrip("/")
        fd_wire_internal_token = e.get("FD_WIRE_INTERNAL_TOKEN", "").strip()
        relay_host = e.get("FD_WIRE_RELAY_HOST", "").strip()
        raw_relay_port = e.get("FD_WIRE_RELAY_PORT_BASE", "18005")
        try:
            relay_port_base = int(raw_relay_port)
        except ValueError as exc:
            raise ConfigError(
                f"FD_WIRE_RELAY_PORT_BASE must be an integer, got {raw_relay_port!r}"
            ) from exc
        if not 1 <= relay_port_base <= 65535:
            raise ConfigError(
                f"FD_WIRE_RELAY_PORT_BASE must be a valid port, got {relay_port_base}"
            )
        raw_poll = e.get("FD_WIRE_RUNNER_INTERVAL", "300")
        try:
            runner_poll_seconds = int(raw_poll)
        except ValueError as exc:
            raise ConfigError(
                f"FD_WIRE_RUNNER_INTERVAL must be an integer, got {raw_poll!r}"
            ) from exc
        if runner_poll_seconds <= 0:
            raise ConfigError(
                f"FD_WIRE_RUNNER_INTERVAL must be > 0, got {runner_poll_seconds}"
            )
        if storage == "postgres":
            missing = [
                name
                for name, value in (
                    ("CELL_PG_ADMIN_URL", pg_admin_url),
                    ("CELL_PG_URL_HOST", pg_url_host),
                    ("CELL_PG_URL_PORT", pg_url_port),
                )
                if not value
            ]
            if missing:
                raise ConfigError(
                    "CELL_STORAGE=postgres requires "
                    + ", ".join(missing)
                    + " to be set (fail-closed；sqlite 画像不受影响)"
                )
        # CELL_OPENBAO_* 已废弃（remove-cell-openbao-dependency）：OpenBAO 实例
        # 退役后不再是生命周期编排的依赖。读取到旧配置不报错、不阻断（升级窗
        # 口的 env.sh 可能残留），只点名告警一次，指引清理。
        stale_openbao = sorted(k for k in e if k.startswith("CELL_OPENBAO_"))
        if stale_openbao:
            logging.getLogger("cell_manager.config").warning(
                "deprecated env ignored (OpenBAO retired, credential custody "
                "now on CELL_SECRETS_DIR file track): %s", ", ".join(stale_openbao)
            )
        return cls(
            home=home,
            data_root=data_root,
            state_file=state_file,
            audit_log=audit_log,
            image=e.get("CELL_IMAGE") or DEFAULT_IMAGE,
            gateway_url=e.get("CELL_GATEWAY_URL") or DEFAULT_GATEWAY_URL,
            network=e.get("CELL_NETWORK") or None,
            registry_url=e.get("CELL_REGISTRY_URL") or DEFAULT_REGISTRY_URL,
            registry_mode=registry_mode,
            target_url_template=e.get("CELL_TARGET_URL_TEMPLATE") or DEFAULT_TARGET_URL_TEMPLATE,
            retention_days=retention_days,
            driver_name=driver_name,
            customer_credential_map=customer_credential_map,
            customer_identity_map=customer_identity_map,
            registry_secret_key_file=e.get("CELL_REGISTRY_SECRET_KEY_FILE", "").strip(),
            preflight_map_path=e.get("CELL_PREFLIGHT_MAP_PATH", "").strip(),
            platform_credential=platform_credential,
            readiness_timeout_seconds=readiness_timeout,
            storage=storage,
            pg_admin_url=pg_admin_url,
            pg_url_host=pg_url_host,
            pg_url_port=pg_url_port,
            secrets_dir=secrets_dir,
            pg_quota_mb=pg_quota_mb,
            fd_wire_internal_url=fd_wire_internal_url,
            fd_wire_internal_token=fd_wire_internal_token,
            relay_host=relay_host,
            relay_port_base=relay_port_base,
            runner_poll_seconds=runner_poll_seconds,
        )

    # ---- runner（add-self-serve-cell-provision 2.4） ----

    def require_runner_settings(self) -> tuple[str, str]:
        """runner 运行前置（fail-closed，缺哪个点名哪个）：返回 (url, token)。"""
        missing = [
            name
            for name, value in (
                ("FD_WIRE_INTERNAL_URL", self.fd_wire_internal_url),
                ("FD_WIRE_INTERNAL_TOKEN", self.fd_wire_internal_token),
                ("FD_WIRE_RELAY_HOST", self.relay_host),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                "cell-manager runner requires " + ", ".join(missing) + " to be set"
            )
        return self.fd_wire_internal_url, self.fd_wire_internal_token

    def relay_port(self, customer_id: str) -> int:
        """每客户 relay 监听口：基口 + crc32(customer_id) 的稳定派生。"""
        validate_customer_id(customer_id)
        import zlib

        return self.relay_port_base + (zlib.crc32(customer_id.encode("utf-8")) % 2000)

    def relay_target_url(self, customer_id: str) -> str:
        """registry 条目的 proxy_pass_url（relay 尾网地址 + 引擎 /mcp 路径）。"""
        return f"http://{self.relay_host}:{self.relay_port(customer_id)}/mcp"

    def registered_endpoint_url(self, customer_id: str) -> str:
        """回写 fd-wire 的客户引擎入口 = registry 公网入口 + 条目路径 + /mcp。"""
        return (
            f"{self.registry_url.rstrip('/')}/{self.registry_path(customer_id)}/mcp"
        )

    # ---- 派生命名（隔离保证：每 cell 独立卷目录 + 独立容器名 + 独立条目路径） ----

    def resolved_secrets_dir(self) -> Path:
        """凭据托管目录（env 未给 CELL_SECRETS_DIR 时缺省 <home>/secrets）。"""
        return self.secrets_dir if self.secrets_dir is not None else self.home / "secrets"

    def volume_dir(self, customer_id: str) -> Path:
        return self.data_root / validate_customer_id(customer_id)

    def container_name(self, customer_id: str) -> str:
        return CELL_NAME_PREFIX + validate_customer_id(customer_id)

    def registry_path(self, customer_id: str) -> str:
        return CELL_NAME_PREFIX + validate_customer_id(customer_id)

    # ---- cell 授权束命名（add-customer-onboarding-automation / ADR 0005） ----
    # 三处派生（组、scope 名、条目可见组）单一来源，杜绝拼字漂移。

    def cell_group(self, customer_id: str) -> str:
        """合成组名：只活在 key 组快照与 scope 文档 group_mappings（不经 Logto）。"""
        return CELL_GROUP_PREFIX + validate_customer_id(customer_id)

    def scope_name(self, customer_id: str) -> str:
        """registry 侧 per-customer scope 文档名（server_access 精确名匹配 cell 条目）。"""
        return CELL_SCOPE_PREFIX + validate_customer_id(customer_id)

    def customer_key_groups(self, customer_id: str) -> list[str]:
        """客户 key 组快照：共享组（业务面 scope）+ 合成组（自己的 cell scope）。"""
        return [WIRE_CUSTOMERS_GROUP, self.cell_group(customer_id)]

    def target_url(self, customer_id: str) -> str:
        return self.target_url_template.format(container_name=self.container_name(customer_id))
