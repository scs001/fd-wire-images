"""cell 侧工具执行出口：``CellMcpExecutor``（ToolExecutor 生产实现）。

把 LLM 的工具调用打到**该企业 cell 的 MCP 端点**；端点由
:func:`resolve_cell_endpoint` 按 customer_id 解析（约定优于配置：
``AI_CELL_ENDPOINT_BASE`` 按 ``fd-wire-cell-<cid>/mcp`` 约定派生，
``AI_CELL_ENDPOINT_MAP`` 显式覆盖优先），未开通即如实报错。

计量归属：本执行器所有调用统一携带**平台服务凭据**
（env ``AI_PLATFORM_CREDENTIAL``，registry patch key）——AI 探索
不计费（计量记录归集平台轨道，凭据是平台身份，绝不落到客户账单）。
计量由**端点**决定：端点必须是 registry 入口形态，容器直连地址
禁止用于客户数据读取（见 fd_mcp_client 包 docstring）。

薄壳：报文/解包/错误体系全部委托 :mod:`fd_mcp_client` native 模式
（标准 MCP：initialize 握手+会话复用+tools/call——registry 定版镜像
对简化报文透传不翻译，2026-10-07 生产实弹重验证）；本模块只保留
env 解析、开通/凭据守卫与领域错误（``CellNotProvisionedError`` /
``CellToolError``）语义。native 客户端跨 ``execute`` 复用（一次握手
多调用）。外部 HTTP 依赖在测试中经注入的 httpx 客户端 mock。
"""

from __future__ import annotations

import httpx
from fd_mcp_client import McpCallError, McpTransportError, NativeMcpClient

from .settings import get_settings, parse_endpoint_map

#: cell 未开通时对话里的常量如实回复（不调 LLM、不产生任何工具执行）
CELL_NOT_PROVISIONED_MESSAGE = (
    "该企业尚未开通数据 cell，AI 构建暂不可用。请联系平台开通 cell 后再试。"
)


class CellNotProvisionedError(RuntimeError):
    """该企业没有 cell MCP 端点（未开通）。"""


class CellToolError(RuntimeError):
    """cell MCP 端点返回非 2xx 或响应不合契约（如实上抛给循环回灌）。"""


#: registry 条目路径约定（与 cell-manager ``registry_path`` 同式）
_CELL_PATH_PREFIX = "fd-wire-cell-"

#: 缺省端点基址 = registry 公网入口（AI_CELL_ENDPOINT_BASE 覆盖）
_DEFAULT_ENDPOINT_BASE = "https://mcp.finddatatech.cloud"


def resolve_cell_endpoint(
    customer_id: str, *, endpoint_map: str | dict[str, str] | None = None
) -> str | None:
    """customer_id → cell MCP 端点；约定优于配置。

    解析序：``AI_CELL_ENDPOINT_MAP`` 显式命中优先（非标部署/测试覆盖）；
    否则按 ``<AI_CELL_ENDPOINT_BASE>/fd-wire-cell-<customer_id>/mcp``
    约定派生（基址缺省 registry 公网入口）。返回 None 仅当 map 已配
    且未命中（显式部署口径下「未开通」），约定派生恒有值——派生端点
    在 cell 未注册时握手期 404/405，由调用方包装「未开通」语义。
    """
    raw = endpoint_map if endpoint_map is not None else get_settings().ai_cell_endpoint_map
    mapping = parse_endpoint_map(raw)
    if mapping:
        return mapping.get(customer_id)
    base = (get_settings().ai_cell_endpoint_base or _DEFAULT_ENDPOINT_BASE).rstrip("/")
    return f"{base}/{_CELL_PATH_PREFIX}{customer_id}/mcp"


class CellMcpExecutor:
    """ToolExecutor 生产实现：工具调用 → 该企业 cell 的 MCP 端点。

    凭据固定走平台服务身份（AI_PLATFORM_CREDENTIAL，见模块 docstring 的
    计量归属说明）；native 客户端跨 ``execute`` 复用（一次握手多调用），
    ``close()`` 尽力注销会话；HTTP 客户端可注入（httpx mock），便于
    全离线测试。
    """

    def __init__(
        self,
        customer_id: str,
        *,
        endpoint: str | None = None,
        credential: str | None = None,
        http_client: httpx.Client | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._customer_id = customer_id
        self._endpoint = endpoint if endpoint is not None else resolve_cell_endpoint(customer_id)
        self._credential = (
            credential if credential is not None else get_settings().ai_platform_credential
        )
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(timeout=timeout)
        self._native: NativeMcpClient | None = None

    @property
    def endpoint(self) -> str | None:
        return self._endpoint

    def close(self) -> None:
        if self._native is not None:
            self._native.close()
        elif self._owns_http:
            self._http.close()

    def execute(self, tool_name: str, args: dict) -> dict:
        """执行一次 cell 工具调用（会话复用），返回 MCP 端点的结果 dict。

        端点契约（native 模式，标准 MCP streamable-http）：initialize
        握手（``Mcp-Session-Id`` 会话凭证）→ ``tools/call``，鉴权
        ``Authorization: Bearer <AI_PLATFORM_CREDENTIAL>``（平台凭据
        轨道，registry 入口计量归集）。

        握手期 404/405（派生端点在 cell 未注册时的真实形态）包装成
        ``CellNotProvisionedError``——如实「未开通」，不静默降级。
        """
        if not self._endpoint:
            raise CellNotProvisionedError(
                f"customer {self._customer_id} 的 cell 未开通（无 MCP 端点）"
            )
        if not self._credential:
            raise CellToolError(
                "平台服务凭据未配置（AI_PLATFORM_CREDENTIAL），拒绝以客户凭据执行 AI 探索"
            )
        if self._native is None:
            self._native = NativeMcpClient(
                self._endpoint,
                credential=self._credential,
                http_client=self._http,
                label="cell MCP 端点",
            )
        try:
            return self._native.call(tool_name, args)
        except McpTransportError as exc:
            text = str(exc)
            if "返回 404" in text or "返回 405" in text:
                raise CellNotProvisionedError(
                    f"customer {self._customer_id} 的 cell 未开通（握手期端点拒绝: {exc}）"
                ) from exc
            raise CellToolError(str(exc)) from exc
        except McpCallError as exc:
            raise CellToolError(f"cell MCP 端点{exc}") from exc

    def list_tool_schemas(self) -> list[dict]:
        """``tools/list`` → OpenAI tools 形态（wire-copilot-action-gate D1：
        副驾循环按引擎可用性动态注入 cell 指标探索工具）。

        端点未配/凭据缺失/发现失败一律返回空列表——cell 工具不进工具面，
        循环照常以读工具+提议面运行（fail-open 到「无引擎工具」而非断话）。
        """
        if not self._endpoint or not self._credential:
            return []
        if self._native is None:
            self._native = NativeMcpClient(
                self._endpoint,
                credential=self._credential,
                http_client=self._http,
                label="cell MCP 端点",
            )
        try:
            tools = self._native.list_tools()
        except Exception:  # noqa: BLE001 - 发现失败=本轮不提供 cell 工具
            return []
        schemas: list[dict] = []
        for tool in tools:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                continue
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": str(tool.get("description") or ""),
                        "parameters": tool.get("inputSchema")
                        or {"type": "object", "properties": {}},
                    },
                }
            )
        return schemas
