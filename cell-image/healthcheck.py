#!/usr/bin/env python3
"""容器 HEALTHCHECK：对 cell 自身 MCP 端点打一次 JSON-RPC initialize。

- ``MCP_TRANSPORT`` 非 http（stdio）时容器没有 HTTP 面 → 退化为进程存活
  语义，直接通过（stdio 形态容器本身以进程退出为健康信号）。
- 端口/主机读 ``MCP_PORT``（默认 8000，与 cell-manager
  ``DEFAULT_TARGET_URL_TEMPLATE`` 的 :8000 对齐）/ 127.0.0.1。
- ``MCP_BEARER_TOKEN`` 设置时（wire→cell 这一跳的入站鉴权）原样附带。

判定：HTTP 200 且响应体形似 JSON-RPC（含 "result" 或 "jsonrpc"）。
stdlib only。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

TIMEOUT = 4  # 秒


def main() -> int:
    if os.environ.get("MCP_TRANSPORT", "http") != "http":
        print("healthcheck: stdio transport (no HTTP face) -> process-liveness only, ok")
        return 0

    host = os.environ.get("MCP_HEALTHCHECK_HOST", "127.0.0.1")
    port = os.environ.get("MCP_PORT", "8000")
    url = f"http://{host}:{port}/mcp"

    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "cell-healthcheck", "version": "0.0.0"},
        },
    }).encode("utf-8")

    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    token = os.environ.get("MCP_BEARER_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            status = resp.status
            raw = resp.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        print(f"healthcheck: initialize -> HTTP {exc.code}: {exc.read()[:200]!r}")
        return 1
    except Exception as exc:  # noqa: BLE001 — 任何传输错误都算不健康
        print(f"healthcheck: initialize failed: {type(exc).__name__}: {exc}")
        return 1

    if status != 200 or not ('"result"' in raw or "jsonrpc" in raw):
        print(f"healthcheck: unexpected response (HTTP {status}): {raw[:200]!r}")
        return 1
    print(f"healthcheck: initialize ok (HTTP {status})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
