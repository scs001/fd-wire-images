"""AI 对话构建循环的运行时配置（wire-platform-v1 任务 5.4）。

全部 env 可覆盖（与仓级「借用契约」纪律一致）：

- ``AI_DATABASE_URL``：对话/消息/产物暂存表连接串，默认本地 SQLite 文件；
  测试指向内存库。
- ``AI_MAX_TURNS``：单次用户消息触发的构建循环轮次上限（LLM↔工具往返
  计数），默认 8——预算护栏之一，达上限如实告知截断。
- ``AI_DAILY_CONVERSATION_LIMIT``：每客户每日新开对话数上限，默认 50；
  超限 ``429`` 带说明（计数=conversation 表按 UTC 日 count）。
- ``TOKEN_GATEWAY_URL`` / ``TOKEN_GATEWAY_KEY`` / ``TOKEN_GATEWAY_MODEL``：
  token 网关 OpenAI 兼容面（LLM 出口，成本平台可控）。
- ``AI_PLATFORM_CREDENTIAL``：平台服务凭据——AI 探索性工具执行统一走
  该凭据（不计费到客户账单；真正计量发生在 registry audit 侧）。
- ``AI_CELL_ENDPOINT_BASE``：cell 端点约定派生的基址（缺省 registry
  公网入口 ``https://mcp.finddatatech.cloud``）；customer_id 的引擎
  端点按 ``<base>/fd-wire-cell-<customer_id>/mcp`` 约定派生。
- ``AI_CELL_ENDPOINT_MAP``：customer_id → cell 端点的**显式覆盖**映射，
  ``cust-001=https://mcp.finddatatech.cloud/fd-wire-cell-cust-001/mcp,cust-002=...``
  形式（也接受 JSON 对象）；命中优先于约定派生，map 已配且未命中即
  该企业 cell 未开通（显式部署口径）。
  map 值**必须**走 registry 入口（``https://<registry>/<server>/mcp``
  形态）——计量归集发生在 registry 审计侧；配成容器直连地址
  （如 ``http://cell-001:8080/mcp``）会让 AI 探索调用绕过计量门，
  一条计量记录都不产生。
- ``AI_AUTO_CREATE_TABLES``：启动时 ``create_all``（默认 true 开发便利；
  生产走 Alembic 迁移链 0005 并可关闭）。
"""

from __future__ import annotations

import json
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


def parse_endpoint_map(raw: str | dict[str, str] | None) -> dict[str, str]:
    """把 env 串（``cust-001=https://...,cust-002=...`` 或 JSON 对象）解析为
    customer_id → cell 的 registry wire 入口 URL 映射（值必须是 registry
    入口而非容器直连地址，否则 AI 探索调用不经计量门——见模块头注）。"""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    text = raw.strip()
    if text.startswith("{"):
        return dict(json.loads(text))
    pairs: dict[str, str] = {}
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"AI_CELL_ENDPOINT_MAP 片段缺少 '=': {chunk!r}")
        key, _, value = chunk.partition("=")
        pairs[key.strip()] = value.strip()
    return pairs


class AiSettings(BaseSettings):
    """AI 构建循环设置：同名环境变量优先。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ai_database_url: str = "sqlite:///./fd_wire_ai.db"
    ai_max_turns: int = 8
    ai_daily_conversation_limit: int = 50
    # token 网关（OpenAI 兼容面）——默认值与仓级 config.settings 同源
    token_gateway_url: str = "https://token.finddatatech.cloud"
    token_gateway_key: str = ""
    token_gateway_model: str = "glm-4.7"
    # 平台服务凭据：AI 探索不计费的凭据轨道
    ai_platform_credential: str = ""
    # cell 端点约定派生基址（缺省 registry 公网入口；map 覆盖优先）
    ai_cell_endpoint_base: str = "https://mcp.finddatatech.cloud"
    ai_cell_endpoint_map: str = ""
    ai_auto_create_tables: bool = True

    def endpoint_map(self) -> dict[str, str]:
        """customer_id → cell MCP 端点（解析后的 dict）。"""
        return parse_endpoint_map(self.ai_cell_endpoint_map)


@lru_cache(maxsize=1)
def get_settings() -> AiSettings:
    """进程内单例；测试用 ``reset_ai_runtime()`` 后重设 env 再取。"""
    return AiSettings()
