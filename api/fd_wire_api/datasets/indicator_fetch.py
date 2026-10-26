"""指标序列拉数（wire-indicator-to-dataset 任务 1.3）。

经 MCP 调上游统一拉数工具 ``indicator_series_read``（business-mcp，
配套 change ``business-mcp-indicator-series-read``），把目录标识
（``source_db:native_code``）的序列落成统一行形状。拉数走平台凭据
出站-only（红线 4/5 对账：不碰客户源凭据；拉数零计费不记 metering）。

行契约（与上游 fixture 双侧锁定，主线程对拍纪律）：
``{indicator_id, date, value, unit, brand: "finddata"}``——``date`` 为
ISO 日期串（年表为当年 12-31）；无效标识/不支持源走 ``errors[]``（批内
隔离），从不伪装空集。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import httpx
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from fd_mcp_client import McpCallError, McpTransportError, NativeMcpClient

from .ingest import EngineCallError, EngineUnavailable

#: 上游工具名（冻结契约；上游改名即 wire 侧 fixture 红）
INDICATOR_SERIES_TOOL = "indicator_series_read"


class IndicatorFetchSettings(BaseSettings):
    """上游拉数通道配置（env 可重指；缺省网内 business-mcp 副本）。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    indicator_fetch_url: str = Field(
        default="http://fd-find-data-business-mcp.fd-mcp.svc:8310",
        validation_alias="INDICATOR_FETCH_URL",
    )
    #: 平台服务凭据（business-mcp 的 internal token 轨；空=匿名仅限本地开发）
    indicator_fetch_token: str = Field(
        default="", validation_alias="INDICATOR_FETCH_TOKEN"
    )
    #: 单次建集的指标数上限（v0 单指标一集，1 即够；留位批量包）
    indicator_fetch_max_ids: int = Field(
        default=10, validation_alias="INDICATOR_FETCH_MAX_IDS"
    )
    #: 时间范围上限（年）——防大范围慢拉楔死建集请求
    indicator_fetch_max_years: int = Field(
        default=30, validation_alias="INDICATOR_FETCH_MAX_YEARS"
    )
    #: 上游调用超时（秒）——冷区间同步拉数分钟级，如实 502 不静默截断
    indicator_fetch_timeout_seconds: float = Field(
        default=120.0, validation_alias="INDICATOR_FETCH_TIMEOUT_SECONDS"
    )

    @property
    def mcp_url(self) -> str:
        url = self.indicator_fetch_url.rstrip("/")
        return url if url.endswith("/mcp") else f"{url}/mcp"


@lru_cache(maxsize=1)
def get_indicator_fetch_settings() -> IndicatorFetchSettings:
    return IndicatorFetchSettings()


@dataclass
class IndicatorSeries:
    """一次拉数的结果：行 + 该标识的错误（批内隔离语义）。"""

    rows: list[dict]
    error: str | None = None


def validate_range(source, *, now=None) -> tuple[str | None, str | None]:
    """时间范围上限校验（422 语义由调用方翻译；超限绝不发起上游拉取）。

    ``start``/``end`` 接受 ``YYYY`` 或 ``YYYY-MM-DD``；两端都缺省 = 全史。
    """
    import re
    from datetime import date

    start, end = source.start, source.end
    pat = re.compile(r"^\d{4}(-\d{2}-\d{2})?$")
    for label, value in (("start", start), ("end", end)):
        if value is not None and not pat.match(value):
            raise ValueError(f"时间范围 {label}={value!r} 不合法（YYYY 或 YYYY-MM-DD）")
    if start and end and start[:4] > end[:4]:
        raise ValueError("时间范围倒置：start 晚于 end")
    settings = get_indicator_fetch_settings()
    if start and end:
        span = int(end[:4]) - int(start[:4]) + 1
        if span > settings.indicator_fetch_max_years:
            raise ValueError(
                f"时间范围 {span} 年超过上限 {settings.indicator_fetch_max_years} 年"
            )
    return start, end


def fetch_series(
    indicator_id: str,
    entity_type: str,
    entity_id: str | int,
    start: str | None,
    end: str | None,
    *,
    settings: IndicatorFetchSettings | None = None,
    http_client: httpx.Client | None = None,
) -> IndicatorSeries:
    """一次上游调用 → 行或错误（单标识；v0 调用方传一个）。

    上游不可达/超时 → ``IndicatorUnavailable``（调用方译 502）；标识级拒绝
    （不支持源/解析失败/实体类型错）→ ``error`` 字段（调用方译 422）。
    """
    cfg = settings or get_indicator_fetch_settings()
    client = NativeMcpClient(
        cfg.mcp_url,
        credential=cfg.indicator_fetch_token or None,
        http_client=http_client or httpx.Client(timeout=cfg.indicator_fetch_timeout_seconds),
        label="指标拉数端点",
    )
    try:
        result = client.call(
            INDICATOR_SERIES_TOOL,
            {
                "indicator_ids": [indicator_id],
                "entity_type": entity_type,
                "entity_id": entity_id,
                "start": start,
                "end": end,
            },
        )
    except McpTransportError as exc:
        raise EngineUnavailable(f"指标拉数端点不可用: {exc}") from exc
    except McpCallError as exc:
        raise EngineCallError(f"指标拉数端点{exc}") from exc
    finally:
        if http_client is None:
            client.close()

    if not isinstance(result, dict):
        raise EngineCallError("指标拉数端点返回非对象载荷")
    errors = result.get("errors") or []
    if errors:
        first = errors[0] if isinstance(errors[0], dict) else {"error": str(errors[0])}
        return IndicatorSeries(rows=[], error=str(first.get("error") or first))
    rows = result.get("results")
    if not isinstance(rows, list):
        raise EngineCallError("指标拉数端点返回缺 results 列表")
    return IndicatorSeries(rows=rows, error=None)
