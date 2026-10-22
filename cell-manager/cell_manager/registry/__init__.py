"""registry client 包：协议 + 工厂。"""

from __future__ import annotations

import os
from typing import Mapping

from cell_manager.config import CellManagerConfig
from cell_manager.errors import ConfigError
from cell_manager.registry.base import RegistryClient, ScopeDoc, ServerEntry
from cell_manager.registry.fake import FakeRegistryClient
from cell_manager.registry.http import HttpRegistryClient, auth_from_env

__all__ = [
    "RegistryClient",
    "ScopeDoc",
    "ServerEntry",
    "FakeRegistryClient",
    "HttpRegistryClient",
    "auth_from_env",
    "build_registry_client",
]


def build_registry_client(
    config: CellManagerConfig, env: Mapping[str, str] | None = None
) -> RegistryClient:
    """按配置构造 registry client（CELL_REGISTRY_MODE=http|fake）。

    fake 模式条目表落盘到 ``<home>/state/fake-registry.json``——CLI 跨进程
    演示时与 cells.json 簿记同步存续（单测直构缺省纯内存）。

    http 模式鉴权从 env 解析（见 ``registry/http.py::auth_from_env``）：
    ``CELL_REGISTRY_TOKEN``（Bearer JWT）或 ``CELL_REGISTRY_COOKIE`` +
    ``CELL_REGISTRY_CSRF``（控制台会话）。
    """
    if config.registry_mode == "fake":
        return FakeRegistryClient(persist_path=config.home / "state" / "fake-registry.json")
    if config.registry_mode == "http":
        e = dict(os.environ if env is None else env)
        timeout_raw = e.get("CELL_REGISTRY_TIMEOUT", "").strip()
        try:
            timeout = float(timeout_raw) if timeout_raw else None
        except ValueError as exc:
            raise ConfigError(f"CELL_REGISTRY_TIMEOUT must be a number, got {timeout_raw!r}") from exc
        return HttpRegistryClient(
            config.registry_url,
            auth=auth_from_env(e),
            register_path=e.get("CELL_REGISTRY_REGISTER_PATH"),
            remove_path=e.get("CELL_REGISTRY_REMOVE_PATH"),
            servers_path=e.get("CELL_REGISTRY_SERVERS_PATH"),
            scope_import_path=e.get("CELL_SCOPE_IMPORT_PATH"),
            scope_delete_path=e.get("CELL_SCOPE_DELETE_PATH"),
            caller_map_path=e.get("CELL_PREFLIGHT_MAP_PATH"),
            **({"timeout": timeout} if timeout is not None else {}),
        )
    raise ConfigError(f"unknown registry mode: {config.registry_mode!r}")
