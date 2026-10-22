"""客户数据集引擎通道：``EngineToolClient``（契约 §3.1，唯一数据通道）。

平台侧**永不直读** cell 数据卷：一切族数据面读写都经该客户端打
**该企业 cell 的 MCP 端点**：

- 端点解析：约定优于配置——``AI_CELL_ENDPOINT_BASE``（缺省 registry
  公网入口）按 ``fd-wire-cell-<customer_id>/mcp`` 约定派生；
  ``AI_CELL_ENDPOINT_MAP``（customer_id → 显式端点）命中优先（非标
  部署/测试覆盖），经 ``ai.executor.resolve_cell_endpoint`` 复用同一
  解析纪律。
- 凭据：``AI_PLATFORM_CREDENTIAL``（平台服务身份=registry patch key，
  计量归集轨，**不计费**）。
- 报文与解包全部委托 :mod:`fd_mcp_client` 的 native 模式
  （:class:`~fd_mcp_client.NativeMcpClient`）：标准 MCP streamable-http，
  initialize 握手 + 会话复用 + ``tools/call``——registry 定版镜像对
  简化报文透传不翻译，标准 JSON-RPC 是唯一活形态（2026-10-07 生产
  实弹重验证）。计量由**端点**决定：registry 入口形态即计量轨，
  容器直连地址禁止用于客户数据（见 fd_mcp_client 包 docstring）。
- 未配凭据一律 ``EngineUnavailable`` → 路由 502 fail-closed，绝不
  静默降级、绝不假成功、绝不改走直连数据卷。

本模块不存任何客户源账密；连接器 key 与配对令牌也绝不出现在这里。
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
from fd_mcp_client import McpCallError, McpTransportError, NativeMcpClient

from ..ai.executor import resolve_cell_endpoint
from ..ai.settings import get_settings as get_ai_settings


class EngineUnavailable(RuntimeError):
    """cell 未开通或平台凭据未配置——fail-closed（502）。"""


class EngineCallError(RuntimeError):
    """引擎端点不可达 / 非 2xx / 响应不合契约 / 工具报错。"""


#: 引擎工具全名（契约 §2.1，customer_dataset 组注册名；拆组后无 daas_ 前缀）
TOOL_INGEST_BEGIN = "customer_dataset_ingest_begin"
TOOL_INGEST_CHUNK = "customer_dataset_ingest_chunk"
TOOL_INGEST_COMMIT = "customer_dataset_ingest_commit"
TOOL_INGEST_ABORT = "customer_dataset_ingest_abort"
TOOL_OVERVIEW = "customer_dataset_overview"
TOOL_PREVIEW = "customer_dataset_preview"
TOOL_CORRECTION_ADD = "customer_dataset_correction_add"
TOOL_CORRECTION_LIST = "customer_dataset_correction_list"
TOOL_CORRECTION_DANGLING = "customer_dataset_correction_dangling"
TOOL_CORRECTION_REVERT = "customer_dataset_correction_revert"
TOOL_DELETE = "customer_dataset_delete"
#: 数据处理引擎（wire-customer-data-engine §1–§4）
TOOL_QUERY = "customer_dataset_query"
TOOL_AGGREGATE = "customer_dataset_aggregate"
TOOL_WRITE_BATCH = "customer_dataset_write_batch"
TOOL_CREATE_DECLARED = "customer_dataset_create_declared"
TOOL_ADD_COLUMN = "customer_dataset_add_column"
TOOL_DERIVE = "customer_dataset_derive"
TOOL_JOIN_INDICATORS = "customer_dataset_join_indicators"
TOOL_DERIVE_REFRESH = "customer_dataset_derive_refresh"
TOOL_LINEAGE = "customer_dataset_lineage"
#: 数据集间连接（wire-entity-link：LEFT JOIN 语义物化，血缘 kind="link"）
TOOL_LINK = "customer_dataset_link"


class EngineToolClient:
    """一次同步/预览/修正操作的引擎调用句柄（HTTP 客户端可注入 mock）。

    薄壳：解析/守卫（env、fail-closed）在本类，报文与解包在
    :mod:`fd_mcp_client` native 模式；``McpTransportError`` /
    ``McpCallError`` 原样包成 ``EngineCallError``，路由行为不变。
    native 客户端惰性建、跨 ``call`` 复用（一次握手多调用），
    ``close()`` 尽力注销会话。
    """

    def __init__(
        self,
        customer_id: str,
        *,
        endpoint: str | None = None,
        credential: str | None = None,
        http_client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._customer_id = customer_id
        self._endpoint = endpoint if endpoint is not None else None
        self._endpoint_explicit = endpoint is not None
        self._credential = credential
        self._credential_explicit = credential is not None
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(timeout=timeout)
        self._native: NativeMcpClient | None = None

    # ------------------------------------------------------------------ 解析

    @property
    def endpoint(self) -> str | None:
        if not self._endpoint_explicit:
            self._endpoint = resolve_cell_endpoint(self._customer_id)
        return self._endpoint

    @property
    def credential(self) -> str | None:
        if not self._credential_explicit:
            self._credential = get_ai_settings().ai_platform_credential
        return self._credential

    def close(self) -> None:
        if self._native is not None:
            self._native.close()
        elif self._owns_http:
            self._http.close()

    # ------------------------------------------------------------------ 调用

    def call(self, tool: str, arguments: dict) -> dict:
        """``tools/call`` 一次（会话复用），返回工具结果 dict；任何异常都不降级。

        握手期 404/405（nginx 无该 server 的 location → 控制台兜底路由）
        包装成 ``EngineUnavailable``（「cell 未开通」语义），与映射未
        命中同语义——如实报错，不静默降级。
        """
        endpoint = self.endpoint
        if not endpoint:
            raise EngineUnavailable(
                f"customer {self._customer_id} 的 cell 未开通（AI_CELL_ENDPOINT_MAP 无映射且约定派生失败）"
            )
        credential = self.credential
        if not credential:
            raise EngineUnavailable(
                "平台服务凭据未配置（AI_PLATFORM_CREDENTIAL），拒绝以其他身份访问客户数据"
            )
        if self._native is None:
            self._native = NativeMcpClient(
                endpoint,
                credential=credential,
                http_client=self._http,
                label="引擎端点",
            )
        try:
            return self._native.call(tool, arguments)
        except McpTransportError as exc:
            if _is_not_provisioned(exc):
                raise EngineUnavailable(
                    f"customer {self._customer_id} 的 cell 未开通（握手期端点拒绝: {exc}）"
                ) from exc
            raise EngineCallError(str(exc)) from exc
        except McpCallError as exc:
            raise EngineCallError(f"引擎{exc}") from exc


#: 握手期「端点不存在」的特征：404（无路由）或 405（控制台兜底路由拒
#: 该方法）——registry 入口对未注册 server 的两种真实形态。
def _is_not_provisioned(exc: McpTransportError) -> bool:
    text = str(exc)
    return "返回 404" in text or "返回 405" in text


#: 引擎客户端工厂（路由依赖注入；测试覆写为 fake 工厂断言调用序列）
EngineClientFactory = Callable[[str], EngineToolClient]


def get_engine_client_factory() -> EngineClientFactory:
    """FastAPI 依赖：按 customer_id 造引擎客户端。"""
    return EngineToolClient
