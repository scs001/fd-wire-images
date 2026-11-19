"""本地 secrets 文件托管（remove-cell-openbao-dependency · 任务 1.1）。

PG 画像的客户库凭据（一 cell 一条目）落 ``<secrets_dir>/<path>.json``（逻辑
路径 ``wire/cell-db/<customer_id>`` → 文件名 ``wire-cell-db-<customer_id>.json``），
0600 权限，与 cell-manager 既有 secrets 文件轨道（wgk key 等）同款做法。
OpenBAO（the node mcp-openbao，已退役）不再是生命周期编排的依赖：
provision / purge 全程不触任何外部凭据服务。

纪律（对齐原 OpenbaoClient 契约）：
- 三件事：``put`` / ``get`` / ``delete``；``get`` 缺失 → ``None``（幂等探测），
  ``delete`` 缺失 → 成功（幂等）。
- 失败即 ``SecretsStoreError``（消息带下一步指引），**不重试不降级**：
  写入失败（目录不可用/磁盘满）时 provision fail-closed，不留半托管态。
- 错误消息绝不包含条目内容（口令不出现在日志/审计/对话）。
- 原子写：临时文件 + ``os.replace``，权限先 ``0o600`` 再落盘。
- path 白名单：仅 ``[A-Za-z0-9._-]`` 与 ``/`` 分隔，拒绝 ``..`` 与绝对路径——
  逻辑路径只能落在 secrets 目录内。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from cell_manager.errors import CellManagerError

#: 逻辑路径白名单：分段字母数字点横线下划线，禁止路径逃逸形态
_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class SecretsStoreError(CellManagerError):
    """secrets 文件托管交互失败（消息自带下一步指引；不含条目明文）。

    继承 ``CellManagerError`` 使 Manager._run 的失败审计与 CLI 的基类捕获
    对存储故障一视同仁（与原 OpenbaoError 形态一致，契约不变）。
    """


def _validate_path(path: str) -> str:
    """校验逻辑路径并原样返回；非法即 ``SecretsStoreError``（启动期 fail-fast）。"""
    if not path or not isinstance(path, str) or path.startswith("/"):
        raise SecretsStoreError(
            f"invalid credential path {path!r}: must be a relative logical path"
        )
    for segment in path.split("/"):
        if segment == ".." or not _PATH_SEGMENT_RE.match(segment):
            raise SecretsStoreError(
                f"invalid credential path {path!r}: segments must match "
                r"[A-Za-z0-9][A-Za-z0-9._-]* (no '..', no absolute paths)"
            )
    return path


class FileSecretStore:
    """极薄文件托管三件套：put/get/delete，失败带指引，不重试不降级。"""

    def __init__(self, secrets_dir: Path | str) -> None:
        self._dir = Path(secrets_dir)

    @property
    def dir(self) -> Path:
        return self._dir

    # ---- 对外三件套 ----

    def put(self, path: str, data: dict[str, Any]) -> None:
        """写入条目（JSON 单文件，0600；原子落盘）。失败抛 ``SecretsStoreError``。"""
        target = self._file_for(path)
        tmp = target.with_suffix(target.suffix + f".tmp.{os.getpid()}")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(dict(data), ensure_ascii=False).encode("utf-8")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
            os.replace(tmp, target)
            # 临时文件权限在创建时已 0600；对已有文件再显式收紧一次（幂等）
            os.chmod(target, 0o600)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:  # noqa: BLE001 —— 清理尽力，原异常优先
                pass
            raise SecretsStoreError(
                f"credential store put {path!r} failed ({type(exc).__name__}: {exc}) — "
                f"地址核对：检查 CELL_SECRETS_DIR（当前 {self._dir}）目录权限与磁盘"
            ) from exc

    def get(self, path: str) -> dict[str, Any] | None:
        """读条目；文件不存在 → ``None``（幂等探测语义），损坏 JSON 抛错。"""
        target = self._file_for(path)
        if not target.is_file():
            return None
        try:
            payload = json.loads(target.read_bytes().decode("utf-8"))
        except (OSError, ValueError) as exc:
            raise SecretsStoreError(
                f"credential store entry {path!r} is unreadable/corrupt "
                f"({type(exc).__name__}: {exc}) — 核对 {target} 后修复或删除重试"
            ) from exc
        if not isinstance(payload, dict):
            raise SecretsStoreError(
                f"credential store entry {path!r} has unexpected payload shape "
                f"(not a JSON object) — 核对 {target} 后修复或删除重试"
            )
        return payload

    def delete(self, path: str) -> None:
        """删除条目（幂等：文件本就不存在 → 成功）。IO 失败抛 ``SecretsStoreError``。"""
        target = self._file_for(path)
        try:
            target.unlink(missing_ok=True)
        except OSError as exc:
            raise SecretsStoreError(
                f"credential store delete {path!r} failed "
                f"({type(exc).__name__}: {exc}) — 核对 {target} 权限"
            ) from exc

    # ---- 内部 ----

    def _file_for(self, path: str) -> Path:
        """逻辑路径 → 文件名（``/`` 换 ``-``，与既有 secrets 文件命名同轨）。"""
        _validate_path(path)
        return self._dir / (path.replace("/", "-") + ".json")
