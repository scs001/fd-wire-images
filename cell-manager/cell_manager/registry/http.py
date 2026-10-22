"""HttpRegistryClient：registry 借用契约的真实 HTTP 实现（stdlib urllib，零依赖）。

对齐 mcp-gateway-registry fork（cheap-1 / https://mcp.finddatatech.cloud）控制台 API，
源码 /Users/chengsishi/code/mcp-gateway-registry（行号以该仓为准）：

- 注册  ``POST /api/servers/register``  registry/api/server_routes.py:4272
  form 表单（application/x-www-form-urlencoded，非 JSON）：
  ``name``/``description``/``path`` 必填，remote 部署 ``proxy_pass_url`` 必填；
  ``overwrite`` 缺省即 true（server_routes.py:4283）——同 path 重复注册是干净覆写，
  即协议要求的幂等。path 无前导斜杠会被服务端补齐（server_routes.py:4429）。
  ``visibility``/``allowed_groups`` Form 字段（server_routes.py:1425-1426）：
  组受限发现面隔离——``group-restricted`` 必须带至少一个组（_validate_visibility_
  and_groups）；不传则条目落库 public。
  成功 201（server_routes.py:4761）；403 = 无 register_service 权限或覆写他人条目
  （server_routes.py:4403/4672）；409 = overwrite=false 撞已存在（server_routes.py:4699）。
- 授权  ``POST /api/servers/groups/import``  registry/api/server_routes.py:5356
  JSON 一次性全量 upsert scope 文档（``server_access``+``group_mappings`` verbatim；
  admin 门 + Bearer 免 CSRF）。**必须 ``create_in_idp:false``**——默认 true 会
  事后尝试去 IdP 建组（非致命吞异常，但没必要）。仓库层对 group_mappings
  不做任何 IdP 校验（scope_repository.py:1126-1145 replace_one upsert）——
  合成组 wire-cust-* 安全。
  对比坑：``POST /api/iam/groups`` 会**忽略** client 的 group_mappings（强制
  ``[name]``，management_routes.py:783）——不要用它建授权文档。
- 删授权 ``DELETE /api/iam/groups/{name}``  registry/api/management_routes.py:847
  admin 门；文档不存在时服务端返回失败形态 → 宽松 False（同 unregister 语义）。
- 铸 key  ``POST /api/patch-keys``  registry/api/patch_key_routes.py:65
  Bearer 自签短命 JWT（groups claims 即快照来源）；响应对 ``{key, info:{key_id}}``,
  明文仅出现这一次。吊销 ``DELETE /api/patch-keys/{id}``：200 已吊 / 404 不存在
  或**跨用户**（owner 限定——owner 之外一律 404 防探测）/ 409 已吊过。
- 启用  ``POST /api/servers/toggle``  registry/api/server_routes.py:4556
  form 表单 ``path`` + ``new_state``（bool 字符串）。**控制台注册创建的条目
  默认 disabled**（`is_enabled=false`，nginx location 整条不生成——仅内部
  通道的注册自动启用，server_routes.py:1960-2071 注释）；「开通即注册、
  注册即可用」的 cell 语义要求 register 后显式启用。幂等：true→true 无害。
  200 = 已处理（含服务端即时健康检查与 nginx 重生成标记）；401 = 鉴权失配。
- 摘除  ``POST /api/servers/remove``  registry/api/server_routes.py:5624
  form 表单 ``path``；200 = 已摘除（server_routes.py:5789），404 = 本就不存在
  （server_routes.py:5673）——宽松返回 False 的语义来源。
- 探测  ``GET /api/servers``  registry/api/server_routes.py:883
  只读列表（``{"servers": [...], "total_count": N}``），联调连通性探测用。

鉴权（可插拔 AuthStrategy，v1 两种形态）：

- **bearer**：``Authorization: Bearer <JWT>``。JWT 在控制台用
  ``POST /api/tokens/generate`` 铸币（server_routes.py:3891，默认 8h、上限 7d）；
  nginx 对 /api/ 做 auth_request → auth_server /validate 校验后转发身份
  （docker/nginx_rev_proxy_http_only.conf:415-470、auth_server/server.py:3756）。
  无会话 cookie 时 CSRF 免检（registry/auth/csrf.py:114-126 verify_csrf_token_flexible）。
- **console-session**：``Cookie: mcp_gateway_session=...``（cookie 名
  registry/core/config.py:209）+ ``X-CSRF-Token`` 头（带会话 cookie 时必检，
  csrf.py:136；token 用 ``GET /api/auth/csrf-token`` 铸币，registry/auth/routes.py:487）。
  registry 无用户名密码 REST 登录（登录只走 OAuth2 浏览器流，
  auth_server/oauth2_providers.yml），故 v1 会话由 env 直接喂 cookie+CSRF。

注意：仓内有 patch-key 通道（registry/api/patch_key_routes.py）但 cheap-1 未部署，
本客户端不假设其存在。

错误处理（如实）：HTTP 非 ok 状态 → ``RegistryError``（带状态码与服务端 reason）；
网络不可达/超时 → ``RegistryError``（带底层原因）；摘除 404 → False 不报错。
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Mapping, Protocol

from cell_manager.errors import ConfigError, RegistryError
from cell_manager.registry.base import ScopeDoc, ServerEntry

DEFAULT_REGISTER_PATH = "/api/servers/register"
DEFAULT_TOGGLE_PATH = "/api/servers/toggle"
DEFAULT_REMOVE_PATH = "/api/servers/remove"
DEFAULT_SERVERS_PATH = "/api/servers"
DEFAULT_SCOPE_IMPORT_PATH = "/api/servers/groups/import"
DEFAULT_SCOPE_DELETE_PATH = "/api/iam/groups"
DEFAULT_TIMEOUT = 15.0  # 秒；注册在服务端是同步写（DB+搜索索引），给足余量

#: 注册条目的默认 description（registry 该字段必填；ServerEntry 协议不含描述）。
DEFAULT_DESCRIPTION = "fd-wire cell entry (managed by cell-manager)"


class AuthStrategy(Protocol):
    """可插拔鉴权：产出附加到每个请求的 header。"""

    def headers(self) -> dict[str, str]:
        ...


class BearerTokenAuth:
    """静态 service token：``Authorization: Bearer <JWT>``（CSRF 免检）。"""

    def __init__(self, token: str) -> None:
        self._token = token

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}


class ConsoleSessionAuth:
    """控制台会话：``Cookie`` + ``X-CSRF-Token``（会话 cookie 在场时 CSRF 必检）。"""

    def __init__(self, cookie: str, csrf_token: str) -> None:
        #: 原样 Cookie 头值，如 ``mcp_gateway_session=<value>``（可含多对 kv）。
        self._cookie = cookie
        self._csrf_token = csrf_token

    def headers(self) -> dict[str, str]:
        return {"Cookie": self._cookie, "X-CSRF-Token": self._csrf_token}


class HttpRegistryClient:
    """urllib 实现的 registry client（真实 HTTP；单测 mock urlopen，集成用 Fake）。"""

    def __init__(
        self,
        base_url: str,
        *,
        auth: AuthStrategy | None = None,
        register_path: str | None = None,
        toggle_path: str | None = None,
        remove_path: str | None = None,
        servers_path: str | None = None,
        scope_import_path: str | None = None,
        scope_delete_path: str | None = None,
        caller_map_path: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._auth = auth
        self._register_path = register_path or DEFAULT_REGISTER_PATH
        self._toggle_path = toggle_path or DEFAULT_TOGGLE_PATH
        self._remove_path = remove_path or DEFAULT_REMOVE_PATH
        self._servers_path = servers_path or DEFAULT_SERVERS_PATH
        self._scope_import_path = scope_import_path or DEFAULT_SCOPE_IMPORT_PATH
        self._scope_delete_path = (scope_delete_path or DEFAULT_SCOPE_DELETE_PATH).rstrip("/")
        self._caller_map_path = (caller_map_path or "").strip() or None
        self._timeout = timeout

    def register(self, entry: ServerEntry) -> None:
        """注册（幂等）+ **启用**（控制台注册创建的条目默认 disabled——
        开通即可用语义要求显式置 true，10-07 生产实证）。"""
        name = entry.display_name or entry.path.strip("/")
        fields: dict[str, str] = {
            "name": name,
            "description": DEFAULT_DESCRIPTION,
            "path": entry.path,
            "proxy_pass_url": entry.target_url,
            "overwrite": "true",
        }
        if entry.allowed_groups:
            # 发现面隔离：group-restricted + 本客户合成组（服务端要求至少一组）
            fields["visibility"] = entry.visibility
            fields["allowed_groups"] = ",".join(entry.allowed_groups)
        self._post_form(self._register_path, fields, ok=(200, 201))
        self._post_form(
            self._toggle_path,
            {"path": entry.path, "new_state": "true"},
            ok=(200,),
        )

    def put_caller_map(self, username: str, value: str) -> None:
        """写入预检计费映射（entrance caller → sub2api 账户），幂等 upsert。

        开户不写这一条 = 新客户在 ``PREFLIGHT_UNMAPPED=deny`` 下从第一次入口
        调用起就被 402 CALLER_NOT_MAPPED 挡住（2026-10-08 生产实弹）——
        开户流程 SHALL NOT 静默跳过本步。

        端点 ``PUT /api/sub2api-caller-map``（admin 门禁 + CSRF；patch key
        无会话 cookie 时 CSRF 免检），JSON body；写入后经服务端 TTL
        （默认 30s）对预检生效，无需重启。
        """
        if self._caller_map_path is None:
            raise RegistryError(
                "caller-map write unavailable: CELL_PREFLIGHT_MAP_PATH not configured"
            )
        self._request(
            "PUT",
            self._caller_map_path,
            data={"username": username, "value": value},
            ok=(200, 201),
        )

    def upsert_scope(self, scope: ScopeDoc) -> None:
        """铸造/更新授权 scope 文档（幂等 upsert，不触碰 IdP）。"""
        payload = {
            "scope_name": scope.scope_name,
            "scope_type": "server_scope",
            "server_access": [
                {
                    "server": scope.server,
                    "methods": list(scope.methods),
                    "tools": list(scope.tools),
                }
            ],
            "group_mappings": [scope.group],
            # 发现面（listing）声明：cell scope 声明自己 cell 可发现。import 端点
            # 缺省会写入空列表（fail-closed 覆盖，客户目录里连自己的 cell 都不见
            # ——2026-10-08 生产实锤）；显式带上，与 server_access 精确对齐。
            "ui_permissions": {"list_service": [scope.server]},
            # 关键：默认 true 会事后尝试去 IdP 建 scope_name 组（吞异常非致命，
            # 但合成组轨道不该惊动 IdP——ADR 0005）。
            "create_in_idp": False,
        }
        self._request(
            "POST", self._scope_import_path, data=payload, ok=(200, 201)
        )

    def delete_scope(self, scope_name: str) -> bool:
        """删除授权 scope 文档；不存在 → 宽松 False（与 unregister 语义一致）。"""
        status = self._request(
            "DELETE", f"{self._scope_delete_path}/{scope_name}", ok=(200, 204, 404)
        )
        return status != 404

    def unregister(self, path: str) -> bool:
        """摘除；404 = 本就不存在 → 宽松返回 False（与 FakeClient 语义一致）。"""
        status = self._post_form(self._remove_path, {"path": path}, ok=(200, 404))
        return status != 404

    def delete_caller_map(self, username: str) -> bool:
        """预检映射摘除（退租收口）；404 = 本就不存在 → 宽松 False。

        与 put_caller_map 成对：退租不摘映射 = 该 username 永久占用映射位，
        且同一客户 id 未来重开时会被旧映射误导（2026-10-09 实证：e2e001
        退租后映射残留）。路径同 CELL_PREFLIGHT_MAP_PATH 派生。
        """
        if not self._caller_map_path:
            raise ConfigError("CELL_PREFLIGHT_MAP_PATH not configured")
        status = self._request(
            "DELETE",
            f"{self._caller_map_path}/{urllib.parse.quote(username, safe='')}",
            ok=(200, 204, 404),
        )
        return status != 404

    def list_servers(self, *, limit: int = 1, include_tools: bool = False) -> dict:
        """只读探测：``GET /api/servers``（部署联调 / 健康检查用；需鉴权）。"""
        query = urllib.parse.urlencode({"limit": limit, "include_tools": str(include_tools).lower()})
        body = self._request(
            "GET", f"{self._servers_path}?{query}", ok=(200,), read_json=True
        )
        return body  # type: ignore[return-value]

    # ---- 内部 ----

    def _post_form(self, path: str, fields: Mapping[str, str], *, ok: tuple[int, ...]) -> int:
        data = urllib.parse.urlencode(fields).encode("utf-8")
        return self._request("POST", path, data=data, ok=ok, content_type="application/x-www-form-urlencoded")

    def _request(
        self,
        method: str,
        path: str,
        *,
        data: bytes | dict | None = None,
        ok: tuple[int, ...],
        content_type: str = "application/json",
        read_json: bool = False,
    ) -> int | dict:
        if isinstance(data, dict):
            data = json.dumps(data).encode("utf-8")
        url = f"{self._base}{path}"
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = content_type
        if self._auth is not None:
            headers.update(self._auth.headers())
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                status = resp.status
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read()
            if status not in ok:
                raise RegistryError(
                    f"registry {method} {url} -> HTTP {status}: {_error_reason(raw)}"
                ) from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", None) or exc
            raise RegistryError(f"registry {method} {url} unreachable: {reason}") from exc
        if status not in ok:
            raise RegistryError(f"registry {method} {url} -> unexpected HTTP {status}")
        if read_json:
            return json.loads(raw.decode("utf-8"))
        return status


def _error_reason(raw: bytes) -> str:
    """从错误响应体提炼 reason（服务端 JSON 带 reason/detail/error，否则取原文截断）。"""
    try:
        doc = json.loads(raw.decode("utf-8"))
        if isinstance(doc, dict):
            for key in ("reason", "detail", "error"):
                if doc.get(key):
                    return str(doc[key])
    except (ValueError, UnicodeDecodeError):
        pass
    return repr(raw[:200])


def auth_from_env(env: Mapping[str, str]) -> AuthStrategy | None:
    """从 env 解析鉴权策略。

    显式 ``CELL_REGISTRY_AUTH=bearer|console-session|none`` 优先；缺省自动嗅探：
    有 ``CELL_REGISTRY_TOKEN`` → bearer；否则有 ``CELL_REGISTRY_COOKIE`` +
    ``CELL_REGISTRY_CSRF`` → console-session；都没有 → None（匿名，写操作将 401/403）。
    """
    explicit = env.get("CELL_REGISTRY_AUTH", "").strip().lower() or None
    token = env.get("CELL_REGISTRY_TOKEN", "").strip()
    cookie = env.get("CELL_REGISTRY_COOKIE", "").strip()
    csrf = env.get("CELL_REGISTRY_CSRF", "").strip()

    mode = explicit or ("bearer" if token else "console-session" if cookie else None)
    if mode in (None, "", "none", "anonymous"):
        return None
    if mode == "bearer":
        if not token:
            raise ConfigError("CELL_REGISTRY_AUTH=bearer but CELL_REGISTRY_TOKEN is empty")
        return BearerTokenAuth(token)
    if mode in ("console-session", "console", "session"):
        if not cookie or not csrf:
            raise ConfigError(
                "console-session auth needs both CELL_REGISTRY_COOKIE and CELL_REGISTRY_CSRF"
            )
        return ConsoleSessionAuth(cookie, csrf)
    raise ConfigError(
        f"CELL_REGISTRY_AUTH must be bearer|console-session|none, got {mode!r}"
    )
