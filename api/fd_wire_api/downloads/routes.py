"""下载端点（wire-connector-distribution 任务 1）：连接器二进制的公开取件面。

目录布局——``FD_WIRE_DOWNLOADS_DIR`` 指向下载根目录，连接器产物在其
``connector/`` 子目录，与 URL 命名空间 ``/api/v1/downloads/connector/*``
一一对应：

    <FD_WIRE_DOWNLOADS_DIR>/connector/manifest.json
    <FD_WIRE_DOWNLOADS_DIR>/connector/fd-wire-connector-<os>-<arch>[.exe]

``manifest.json`` 由 CI 晋升或手动晋升时生成（步骤与 sha256 校验命令见
deploy/README.md「连接器取件与晋升」）；端点只做**读取 + 一致性校验**
（manifest 声明的文件缺失 → 该平台 ``available=false``，其余平台不受
影响），不生成、不修复——两条晋升路径以 manifest 的 sha256 为唯一事实源。

诚实降级：``FD_WIRE_DOWNLOADS_DIR`` 未配置、目录无 manifest、单平台文件
缺失，一律如实说明（404 detail 带原因 / available=false），不假装有发布页。

鉴权：无（公开取件——下载件不含敏感数据，manifest 版本号按公开口径呈现；
与 /api/v1/plans 同款公开端点模式）。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

router = APIRouter(tags=["downloads"])

# platform 形如 linux-amd64 / darwin-arm64 / windows-amd64（小写 + 连字符；
# 不含扩展名——exe 后缀属于 filename，不进 URL）。同时天然排除 manifest.json
# 被 /{platform} 路由误吞（带点即不匹配）。
_PLATFORM_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class ConnectorPlatformOut(BaseModel):
    """单平台条目：available = 声明文件在目录中在场（端点逐请求现查）。"""

    platform: str
    filename: str
    sha256: str
    size: int
    available: bool


class ConnectorManifestOut(BaseModel):
    """下载清单（版本 / 发布时间 / 平台条目）；released_at 为晋升时写入。"""

    version: str
    released_at: str
    platforms: list[ConnectorPlatformOut]


def _downloads_root() -> Path | None:
    """FD_WIRE_DOWNLOADS_DIR（请求时读 env，测试可逐用例注入）；未配置 → None。"""
    raw = os.getenv("FD_WIRE_DOWNLOADS_DIR", "").strip()
    return Path(raw) if raw else None


def _not_configured() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=(
            "连接器下载目录未配置（FD_WIRE_DOWNLOADS_DIR 为空）："
            "本部署尚未接入发布件，请按 deploy/README.md「连接器取件与晋升」"
            "手动晋升，或联系支持。"
        ),
    )


def _read_manifest() -> tuple[Path, ConnectorManifestOut]:
    """读盘上 manifest 并做文件在场校验；不可用一律 404 且 detail 说明原因。"""
    root = _downloads_root()
    if root is None:
        raise _not_configured()
    manifest_path = root / "connector" / "manifest.json"
    if not manifest_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"下载目录已配置但 manifest 缺失（{manifest_path}）："
                "尚未执行晋升步骤，请按 deploy/README.md 生成 manifest。"
            ),
        )
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        platforms = [
            ConnectorPlatformOut(
                platform=str(item["platform"]),
                filename=str(item["filename"]),
                sha256=str(item["sha256"]),
                size=int(item["size"]),
                # 一致性校验：声明的文件缺失 → 该平台不可用，不影响其余条目
                available=(root / "connector" / str(item["filename"])).is_file(),
            )
            for item in raw["platforms"]
        ]
        manifest = ConnectorManifestOut(
            version=str(raw["version"]),
            released_at=str(raw["released_at"]),
            platforms=platforms,
        )
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"manifest.json 结构不合法（{manifest_path}）：{exc}",
        ) from exc
    return root / "connector", manifest


@router.get(
    "/api/v1/downloads/connector/manifest.json",
    response_model=ConnectorManifestOut,
)
def connector_manifest() -> ConnectorManifestOut:
    """连接器下载清单（公开；逐平台 available 现查目录，缺文件不整页 404）。"""
    _, manifest = _read_manifest()
    return manifest


@router.get("/api/v1/downloads/connector/{platform}")
def download_connector(platform: str) -> FileResponse:
    """按平台流式返回二进制（Content-Length 由 FileResponse 按文件大小设置，
    附 ``X-Checksum-Sha256`` 供客户端下载后校验——与 manifest 同源）。"""
    if not _PLATFORM_RE.match(platform):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"未知平台 {platform!r}（应为 linux-amd64 / darwin-arm64 等）",
        )
    connector_dir, manifest = _read_manifest()
    entry = next(
        (p for p in manifest.platforms if p.platform == platform), None
    )
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"平台 {platform} 不在当前 manifest（版本 {manifest.version}）："
                "可能暂未发布该平台产物。"
            ),
        )
    path = connector_dir / entry.filename
    # 防御：filename 来自 manifest（晋升方控制），仍拒绝任何路径穿越形态
    if Path(entry.filename).name != entry.filename or not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"平台 {platform} 的文件缺失（{path}）：该条目在 manifest 中"
                "标记为不可用，请等待重新晋升。"
            ),
        )
    return FileResponse(
        path,
        filename=entry.filename,
        headers={"X-Checksum-Sha256": entry.sha256},
    )
