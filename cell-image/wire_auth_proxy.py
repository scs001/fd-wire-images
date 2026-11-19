#!/usr/bin/env python3
"""In-cell credential-injecting reverse proxy for the wire metering gate.

Why this exists (wire-platform-v1 任务 5.3 摸底结论):

- DAAS 引擎的数据读取委托走 ``gateway_upstreams`` 表里名为
  ``fd-open-data-mcp`` 的 upstream 行（daas.db，随 DAAS_DATABASE_URL）。
  http 形态下 DAAS 用 ``Client(StreamableHttpTransport(url))`` 建客户端
  —— **只有 URL、没有 header 面**（gateway_client_pool/client_pool.py
  ``_build_client_from_row``、gateway_database.py ``build_client``）。
- registry wire 入口（mcp-gateway-registry fork）鉴权只认 header：
  ``Authorization: Bearer <per-user 长命 key>`` 或 ``X-Api-Key``，
  不支持 URL query 传 key（auth_server/server.py + nginx /validate 子请求）。
- 引擎不许改动（与謙面本地产品零 diff）→ 在 cell 内垫一个本机回环代理：
  引擎无凭据打到 ``http://127.0.0.1:8312/mcp``，代理补上
  ``Authorization: Bearer <WIRE_CUSTOMER_KEY>`` 再转发 wire 门，计量归属
  由凭据轨道决定。

双凭据轨道（同一进程、按路径路由）::

    /mcp          → WIRE_CUSTOMER_KEY（默认轨：物化读取，计费到客户）
    /mcp-platform → WIRE_PLATFORM_KEY（平台轨：平台成本，归集不结算；
                    DAAS 网关目前只指默认轨——引擎把 upstream 名字
                    "fd-open-data-mcp" 写死、一行一凭据，平台轨留给
                    5.4 executor / 5.6 调度器直连使用，见 README 边界说明）

Stdlib only（http.server + http.client），无新增镜像依赖；支持 MCP
Streamable HTTP 的 POST(JSON / JSON+SSE)、GET(SSE)、DELETE（会话关闭）
——响应逐块透传不缓存，长连接 SSE 不会被代理截断。

Env::

    WIRE_UPSTREAM_URL   目标 wire 门完整 URL（entrypoint 已拼好；优先级最高）
    WIRE_GATEWAY_URL    门基址 + WIRE_SERVER_PATH（默认 fd-find-data-business-mcp/mcp）
                        ——仅当 WIRE_UPSTREAM_URL 未设时使用
    WIRE_CUSTOMER_KEY   客户长命 key（默认轨凭据）
    WIRE_PLATFORM_KEY   平台服务凭据（平台轨；未设则平台轨 503 fail-closed）
    WIRE_PROXY_HOST     监听地址（默认 127.0.0.1，仅回环，不对外）
    WIRE_PROXY_PORT     监听端口（默认 8312）
"""
from __future__ import annotations

import http.client
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

#: 路由表：路径前缀 → (env 变量名, 轨道名)。前缀在转发时被剥掉，
#: 余下路径拼到 wire 门 URL 之后（engine 侧 fastmcp 只会打 /mcp 本身，
#: 余量留给 query / 未来子路径）。
ROUTES = {
    "/mcp": ("WIRE_CUSTOMER_KEY", "customer"),
    "/mcp-platform": ("WIRE_PLATFORM_KEY", "platform"),
}

#: 客户端→上游方向不透传的 hop-by-hop 头（RFC 7230 §6.1 + Host 单独处理）.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}


def resolve_target() -> tuple[str, str, int, str]:
    """解析 wire 门 URL → (scheme, host, port, base_path)。"""
    url = os.environ.get("WIRE_UPSTREAM_URL", "").strip()
    if not url:
        base = os.environ.get("WIRE_GATEWAY_URL", "").strip().rstrip("/")
        suffix = os.environ.get("WIRE_SERVER_PATH", "fd-find-data-business-mcp/mcp").strip("/")
        if not base:
            raise SystemExit("[wire-auth-proxy] neither WIRE_UPSTREAM_URL nor WIRE_GATEWAY_URL set")
        url = f"{base}/{suffix}"
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise SystemExit(f"[wire-auth-proxy] invalid wire gate URL: {url!r}")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"
    return parts.scheme, parts.hostname, port, path


TARGET = resolve_target()
#: 已注入凭据的轨道（启动即定，运行期热读 env 不做——key 轮换走容器重启）
KEYS = {route: os.environ.get(var, "").strip() for route, (var, _) in ROUTES.items()}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # 引擎/fastmcp 的业务日志走 uvicorn，这里只留一行代理迹（stderr→容器日志）

    def log_message(self, fmt: str, *args) -> None:  # noqa: D102
        sys.stderr.write("[wire-auth-proxy] %s\n" % (fmt % args))

    # ---- 路由与凭据 ----

    def _route(self) -> tuple[str | None, str | None, str | None]:
        """incoming path → (剩余路径, 轨道名, 凭据)；未匹配路由返回全 None。"""
        path = self.path.split("?", 1)[0]
        for prefix in sorted(ROUTES, key=len, reverse=True):
            if path == prefix:
                return "", ROUTES[prefix][1], KEYS[prefix]
            if path.startswith(prefix + "/"):
                return path[len(prefix):], ROUTES[prefix][1], KEYS[prefix]
        return None, None, None

    # ---- 通用转发 ----

    def _forward(self) -> None:
        remainder, track, key = self._route()
        if remainder is None:
            body = json.dumps({"error": f"unknown route {self.path!r}; use /mcp or /mcp-platform"}).encode()
            self._reply_raw(404, body, "application/json")
            return

        if not key:
            self._reply_raw(
                503,
                json.dumps({"error": f"credential for track {track!r} not configured (fail-closed)"}).encode(),
                "application/json",
            )
            return

        scheme, host, port, base_path = TARGET
        upstream_path = base_path + remainder
        if "?" in self.path:
            upstream_path += "?" + self.path.split("?", 1)[1]

        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        headers["Host"] = host
        headers["Authorization"] = f"Bearer {key}"
        # Authorization 由代理覆盖（客户端即便带了也以容器 env 为准——凭据
        # 属于 cell 平台面，不属于引擎进程面）

        body = self._read_body()
        if body is None:
            return  # 已回 4xx

        conn_cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(host, port, timeout=300)  # SSE 长流：读超时放宽
        try:
            conn.request(self.command, upstream_path, body=body, headers=headers)
            resp = conn.getresponse()
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                lk = k.lower()
                if lk in ("transfer-encoding", "content-length", "connection"):
                    continue
                if lk == "mcp-session-id":
                    self.send_header(k, v)  # 会话头必须原样带回客户端
                    continue
                self.send_header(k, v)
            streaming = True
            if resp.length is not None:  # 非流式（定长 JSON 响应）
                self.send_header("Content-Length", str(resp.length))
                streaming = False
            else:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # 客户端断开（SSE 取消等），静默收尾
        except OSError as exc:
            sys.stderr.write(f"[wire-auth-proxy] upstream error: {type(exc).__name__}: {exc}\n")
            try:
                self._reply_raw(502, b'{"error": "wire gate unreachable"}', "application/json")
            except Exception:  # noqa: BLE001
                pass
        finally:
            conn.close()

    def _read_body(self) -> bytes | None:
        """读定长请求体；客户端 chunked 体不支持（fastmcp 只发定长）。"""
        length = self.headers.get("Content-Length")
        if not length:
            return b""
        try:
            n = int(length)
        except ValueError:
            self._reply_raw(400, b'{"error": "bad Content-Length"}', "application/json")
            return None
        return self.rfile.read(n) if n > 0 else b""

    def _reply_raw(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- HTTP 方法（/healthz 探活 + 全方法转发） ----

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] == "/healthz":
            self._reply_raw(200, b'{"status": "ok"}', "application/json")
            return
        self._forward()

    def do_POST(self) -> None:  # noqa: N802
        self._forward()

    def do_DELETE(self) -> None:  # noqa: N802
        self._forward()

    def do_HEAD(self) -> None:  # noqa: N802
        self._forward()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._forward()


def main() -> None:
    host = os.environ.get("WIRE_PROXY_HOST", "127.0.0.1")
    port = int(os.environ.get("WIRE_PROXY_PORT", "8312"))
    scheme, gate_host, gate_port, _ = TARGET
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    missing = [f"{var}({track})" for route, (var, track) in ROUTES.items() if not KEYS.get(route)]
    sys.stderr.write(
        f"[wire-auth-proxy] listening on {host}:{port} -> {scheme}://{gate_host}:{gate_port}"
        f" (routes: /mcp=customer, /mcp-platform=platform; missing keys: {missing or 'none'})\n"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
