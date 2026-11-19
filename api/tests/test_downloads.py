"""连接器下载端点三态（wire-connector-distribution 任务 1.2 / 5.1）。

覆盖：
- 未配置 ``FD_WIRE_DOWNLOADS_DIR``：manifest 与二进制端点均 404，detail
  说明原因（诚实降级，不假装有发布页）；
- 已配置但 manifest 缺失：404 带晋升指引；
- 正常态：manifest 形状（version / released_at / 平台条目 sha256+size），
  二进制端点 Content-Length 与 ``X-Checksum-Sha256`` 与文件字节一致；
- 一致性校验：manifest 声明的平台文件缺失 → 该条目 ``available=false``
  其余不受影响（manifest 不整页 404）；缺失平台的二进制端点 404；
- 公开口径：全部请求不带任何 Authorization 头（版本号对外可见性按公开
  端点语义断言——与 /api/v1/plans 同款模式）。
"""

from __future__ import annotations

import hashlib
import json

from fastapi.testclient import TestClient

from fd_wire_api.app import app

client = TestClient(app)


def _write_manifest(root, *, version: str, platforms: list[dict]) -> None:
    connector_dir = root / "connector"
    connector_dir.mkdir(parents=True, exist_ok=True)
    (connector_dir / "manifest.json").write_text(
        json.dumps(
            {
                "version": version,
                "released_at": "2026-10-11T00:00:00Z",
                "platforms": platforms,
            }
        ),
        encoding="utf-8",
    )


def _platform_entry(root, filename: str, platform: str, payload: bytes) -> dict:
    connector_dir = root / "connector"
    connector_dir.mkdir(parents=True, exist_ok=True)
    (connector_dir / filename).write_bytes(payload)
    return {
        "platform": platform,
        "filename": filename,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
    }


# ------------------------------------------------------------ 未配置（404）


def test_manifest_404_when_not_configured(monkeypatch) -> None:
    monkeypatch.delenv("FD_WIRE_DOWNLOADS_DIR", raising=False)
    resp = client.get("/api/v1/downloads/connector/manifest.json")
    assert resp.status_code == 404
    # 错误说明原因（未配置 env），给出口径而不是空 404
    assert "FD_WIRE_DOWNLOADS_DIR" in resp.json()["detail"]
    binary = client.get("/api/v1/downloads/connector/linux-amd64")
    assert binary.status_code == 404
    assert "FD_WIRE_DOWNLOADS_DIR" in binary.json()["detail"]


def test_manifest_404_when_configured_but_missing_manifest(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("FD_WIRE_DOWNLOADS_DIR", str(tmp_path))
    resp = client.get("/api/v1/downloads/connector/manifest.json")
    assert resp.status_code == 404
    assert "manifest" in resp.json()["detail"]


def test_binary_404_for_unknown_or_malformed_platform(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FD_WIRE_DOWNLOADS_DIR", str(tmp_path))
    _write_manifest(
        tmp_path,
        version="sha-abc1234",
        platforms=[_platform_entry(
            tmp_path,
            filename="fd-wire-connector-linux-amd64",
            platform="linux-amd64",
            payload=b"elf-bytes",
        )],
    )
    # 未在 manifest 中声明的平台
    unknown = client.get("/api/v1/downloads/connector/plan9-386")
    assert unknown.status_code == 404
    assert "不在当前 manifest" in unknown.json()["detail"]
    # 畸形 platform（路径穿越 / 带点 / 下划线）在正则闸即拒；
    # 注：字面 manifest.json 由先注册的精确路由承接（返回清单本体，属预期）
    for bad in ("..%2Fetc%2Fpasswd", "notes.txt", "linux_amd64"):
        resp = client.get(f"/api/v1/downloads/connector/{bad}")
        assert resp.status_code == 404, bad


# ------------------------------------------------- 正常 / 部分缺失（三态）


def test_manifest_and_binary_happy_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FD_WIRE_DOWNLOADS_DIR", str(tmp_path))
    payload = b"\x7fELF-fake-connector-bytes"
    _write_manifest(
        tmp_path,
        version="sha-abc1234",
        platforms=[_platform_entry(
            tmp_path,
            filename="fd-wire-connector-linux-amd64",
            platform="linux-amd64",
            payload=payload,
        )],
    )

    manifest = client.get("/api/v1/downloads/connector/manifest.json")
    assert manifest.status_code == 200
    body = manifest.json()
    assert body["version"] == "sha-abc1234"
    assert body["released_at"] == "2026-10-11T00:00:00Z"
    assert [p["platform"] for p in body["platforms"]] == ["linux-amd64"]
    entry = body["platforms"][0]
    assert entry["filename"] == "fd-wire-connector-linux-amd64"
    assert entry["available"] is True
    assert entry["size"] == len(payload)
    assert entry["sha256"] == hashlib.sha256(payload).hexdigest()

    binary = client.get("/api/v1/downloads/connector/linux-amd64")
    assert binary.status_code == 200
    assert binary.content == payload
    # 流式返回带 Content-Length 与 sha256 头（与 manifest 同源）
    assert int(binary.headers["content-length"]) == len(payload)
    assert binary.headers["x-checksum-sha256"] == entry["sha256"]
    assert (
        binary.headers["content-disposition"]
        == 'attachment; filename="fd-wire-connector-linux-amd64"'
    )


def test_missing_platform_file_marks_unavailable_not_page_404(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("FD_WIRE_DOWNLOADS_DIR", str(tmp_path))
    present = _platform_entry(
        tmp_path,
        filename="fd-wire-connector-linux-amd64",
        platform="linux-amd64",
        payload=b"elf-bytes",
    )
    # darwin 条目声明了但文件不落盘（晋升中断 / 人为删除）
    missing = {
        "platform": "darwin-arm64",
        "filename": "fd-wire-connector-darwin-arm64",
        "sha256": "0" * 64,
        "size": 4096,
    }
    _write_manifest(
        tmp_path, version="sha-abc1234", platforms=[present, missing]
    )

    manifest = client.get("/api/v1/downloads/connector/manifest.json")
    assert manifest.status_code == 200  # 缺单平台不整页 404
    by_platform = {
        p["platform"]: p for p in manifest.json()["platforms"]
    }
    assert by_platform["linux-amd64"]["available"] is True
    assert by_platform["darwin-arm64"]["available"] is False

    # 缺失平台的二进制端点如实 404；在场平台不受影响
    missing_binary = client.get("/api/v1/downloads/connector/darwin-arm64")
    assert missing_binary.status_code == 404
    assert "缺失" in missing_binary.json()["detail"]
    ok_binary = client.get("/api/v1/downloads/connector/linux-amd64")
    assert ok_binary.status_code == 200
    assert ok_binary.content == b"elf-bytes"


def test_public_no_auth_header_required(tmp_path, monkeypatch) -> None:
    """公开取件语义：无 Authorization 头可取 manifest（版本号按公开口径呈现）。"""
    monkeypatch.setenv("FD_WIRE_DOWNLOADS_DIR", str(tmp_path))
    _write_manifest(
        tmp_path,
        version="sha-public1",
        platforms=[_platform_entry(
            tmp_path,
            filename="fd-wire-connector-windows-amd64.exe",
            platform="windows-amd64",
            payload=b"mz-bytes",
        )],
    )
    # client 不带任何头：两端口均 200（连接器 key / 登录态对下载面无要求）
    assert (
        client.get("/api/v1/downloads/connector/manifest.json").status_code == 200
    )
    binary = client.get("/api/v1/downloads/connector/windows-amd64")
    assert binary.status_code == 200
    assert binary.content == b"mz-bytes"
    manifest_body = client.get(
        "/api/v1/downloads/connector/manifest.json"
    ).json()
    assert manifest_body["version"] == "sha-public1"
