"""AI 切片基础设施测试：配置解析、cell 端点解析、CellMcpExecutor、
TokenGatewayLlmClient、产物工具 schema/校验/暂存、Alembic 0005 迁移。

外部 HTTP（cell MCP / token 网关）全部经注入的 httpx.MockTransport mock，
绝不打真网。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.ai.db import AiBase, reset_ai_runtime
from fd_wire_api.ai.executor import (
    CellMcpExecutor,
    CellNotProvisionedError,
    CellToolError,
    resolve_cell_endpoint,
)
from fd_wire_api.ai.llm import LlmGatewayError, TokenGatewayLlmClient
from fd_wire_api.ai.models import AIDraftArtifact
from fd_wire_api.ai.settings import get_settings, parse_endpoint_map
from fd_wire_api.ai.tools import (
    ARTIFACT_TOOL_KINDS,
    TOOL_SCHEMAS,
    stage_artifact,
    validate_artifact_args,
)

API_DIR = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ settings


def test_settings_defaults(monkeypatch) -> None:
    for name in (
        "AI_MAX_TURNS",
        "AI_DAILY_CONVERSATION_LIMIT",
        "AI_CELL_ENDPOINT_MAP",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_ai_runtime()
    s = get_settings()
    assert s.ai_max_turns == 8
    assert s.ai_daily_conversation_limit == 50
    assert s.endpoint_map() == {}
    reset_ai_runtime()


def test_settings_env_override_and_endpoint_map(monkeypatch) -> None:
    monkeypatch.setenv("AI_MAX_TURNS", "3")
    monkeypatch.setenv("AI_DAILY_CONVERSATION_LIMIT", "7")
    monkeypatch.setenv("AI_CELL_ENDPOINT_MAP", "cust-001=http://cell-1/mcp,cust-002=http://cell-2/mcp")
    reset_ai_runtime()
    s = get_settings()
    assert s.ai_max_turns == 3
    assert s.ai_daily_conversation_limit == 7
    assert s.endpoint_map() == {
        "cust-001": "http://cell-1/mcp",
        "cust-002": "http://cell-2/mcp",
    }
    reset_ai_runtime()


def test_parse_endpoint_map_forms() -> None:
    assert parse_endpoint_map(None) == {}
    assert parse_endpoint_map("") == {}
    assert parse_endpoint_map({"a": "http://a"}) == {"a": "http://a"}
    assert parse_endpoint_map('{"a": "http://a"}') == {"a": "http://a"}
    with pytest.raises(ValueError):
        parse_endpoint_map("no-equals-sign")


def test_resolve_cell_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("AI_CELL_ENDPOINT_MAP", "cust-001=http://cell-1/mcp")
    reset_ai_runtime()
    assert resolve_cell_endpoint("cust-001") == "http://cell-1/mcp"
    # map 已配且未命中 → None（显式部署口径：未开通，调用方如实报错）
    assert resolve_cell_endpoint("cust-999") is None
    reset_ai_runtime()


def test_resolve_cell_endpoint_convention_derivation(monkeypatch) -> None:
    """约定派生：map 未配时按 ``<base>/fd-wire-cell-<cid>/mcp``（基址可覆盖）。"""
    monkeypatch.delenv("AI_CELL_ENDPOINT_MAP", raising=False)
    monkeypatch.setenv("AI_CELL_ENDPOINT_BASE", "https://registry.example")
    reset_ai_runtime()
    assert (
        resolve_cell_endpoint("cust-042")
        == "https://registry.example/fd-wire-cell-cust-042/mcp"
    )
    # 基址缺省 = registry 公网入口
    monkeypatch.delenv("AI_CELL_ENDPOINT_BASE", raising=False)
    reset_ai_runtime()
    assert (
        resolve_cell_endpoint("cust-042")
        == "https://mcp.finddatatech.cloud/fd-wire-cell-cust-042/mcp"
    )
    reset_ai_runtime()


# ------------------------------------------------------------------ executor


def test_cell_executor_not_provisioned(monkeypatch) -> None:
    """显式部署口径：map 已配且未命中 → 端点 None → CellNotProvisionedError。"""
    monkeypatch.setenv("AI_CELL_ENDPOINT_MAP", "cust-001=http://cell-1/mcp")
    reset_ai_runtime()
    try:
        executor = CellMcpExecutor("cust-999", endpoint=None)
        assert executor.endpoint is None
        with pytest.raises(CellNotProvisionedError):
            executor.execute("search_indicators", {"q": "GDP"})
    finally:
        reset_ai_runtime()


def test_cell_executor_handshake_404_is_not_provisioned(monkeypatch) -> None:
    """约定派生口径：派生端点握手期 404/405 → CellNotProvisionedError（未开通）。"""
    monkeypatch.delenv("AI_CELL_ENDPOINT_MAP", raising=False)
    reset_ai_runtime()
    try:
        for status in (404, 405):
            executor = CellMcpExecutor(
                "cust-nada",
                credential="platform-secret",
                http_client=httpx.Client(
                    transport=httpx.MockTransport(
                        lambda r, s=status: httpx.Response(s, text="no such server")
                    )
                ),
            )
            with pytest.raises(CellNotProvisionedError, match="cell 未开通"):
                executor.execute("search_indicators", {"q": "GDP"})
            executor.close()
    finally:
        reset_ai_runtime()


def test_cell_executor_refuses_without_platform_credential() -> None:
    """平台服务凭据未配置时拒绝执行——绝不悄悄落到客户凭据轨道。"""
    executor = CellMcpExecutor(
        "cust-001",
        endpoint="http://cell-1/mcp",
        credential="",
        http_client=httpx.Client(),
    )
    with pytest.raises(CellToolError, match="平台服务凭据未配置"):
        executor.execute("search_indicators", {"q": "GDP"})


def test_cell_executor_calls_native_mcp_with_platform_credential() -> None:
    """执行走标准 MCP：initialize 握手 → tools/call（工具名进 JSON-RPC
    params），平台凭据 Bearer 上行；会话跨 execute 复用。"""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(
            {
                "url": str(request.url),
                "auth": request.headers.get("Authorization"),
                "method": body.get("method"),
                "body": body,
            }
        )
        if body.get("method") == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "sess-exec"},
                json={"jsonrpc": "2.0", "id": body["id"], "result": {}},
            )
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"structuredContent": {"rows": 3, "preview": [1, 2]}},
            },
        )

    executor = CellMcpExecutor(
        "cust-001",
        endpoint="http://cell-1/mcp",
        credential="platform-secret",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    try:
        result = executor.execute("search_indicators", {"q": "GDP 人均"})
        assert result == {"rows": 3, "preview": [1, 2]}
        assert [s["method"] for s in seen] == [
            "initialize",
            "notifications/initialized",
            "tools/call",
        ]
        assert seen[0]["url"] == "http://cell-1/mcp"
        assert seen[0]["auth"] == "Bearer platform-secret"
        call = seen[2]
        assert call["body"]["params"] == {"name": "search_indicators", "arguments": {"q": "GDP 人均"}}
        assert call["body"]["jsonrpc"] == "2.0"
        # 会话复用：第二次 execute 不再握手
        executor.execute("search_indicators", {"q": "CPI"})
        assert [s["method"] for s in seen].count("initialize") == 1
    finally:
        executor.close()


def test_cell_executor_tool_errors_are_cell_tool_error() -> None:
    """JSON-RPC error / isError / 非 2xx / 非 JSON / 形态不合契约 → CellToolError。"""

    def _handshake_then(response_factory):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if body.get("method") == "initialize":
                return httpx.Response(
                    200,
                    headers={"Mcp-Session-Id": "s"},
                    json={"jsonrpc": "2.0", "id": body["id"], "result": {}},
                )
            if body.get("method") == "notifications/initialized":
                return httpx.Response(202)
            return response_factory(body)

        return handler

    cases = [
        (lambda b: httpx.Response(200, json={"jsonrpc": "2.0", "id": b["id"], "error": {"code": -32601, "message": "no such tool"}}), "no such tool"),
        (lambda b: httpx.Response(200, json={"result": {"structuredContent": {"ok": True}}}), "ok"),  # 对照：正常路径
    ]
    # JSON-RPC error → CellToolError
    executor = CellMcpExecutor(
        "cust-001",
        endpoint="http://cell-1/mcp",
        credential="platform-secret",
        http_client=httpx.Client(transport=httpx.MockTransport(_handshake_then(cases[0][0]))),
    )
    with pytest.raises(CellToolError, match="no such tool"):
        executor.execute("t", {})
    executor.close()

    # 正常路径对照
    executor2 = CellMcpExecutor(
        "cust-001",
        endpoint="http://cell-1/mcp",
        credential="platform-secret",
        http_client=httpx.Client(transport=httpx.MockTransport(_handshake_then(cases[1][0]))),
    )
    assert executor2.execute("t", {}) == {"ok": True}
    executor2.close()

    # 非 2xx / 不可达 / 非 JSON / 形态不合契约 → CellToolError（如实上抛）
    for response, pattern in [
        (httpx.Response(500, text="boom"), "返回 500"),
        (httpx.Response(200, text="not-json"), "非 JSON"),
        (httpx.Response(200, json=[1, 2]), "非 JSON"),
    ]:
        bad = CellMcpExecutor(
            "cust-001",
            endpoint="http://cell-1/mcp",
            credential="platform-secret",
            http_client=httpx.Client(
                transport=httpx.MockTransport(lambda r, resp=response: resp)
            ),
        )
        with pytest.raises(CellToolError, match=pattern):
            bad.execute("t", {})

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    offline = CellMcpExecutor(
        "cust-001",
        endpoint="http://cell-1/mcp",
        credential="platform-secret",
        http_client=httpx.Client(transport=httpx.MockTransport(unreachable)),
    )
    with pytest.raises(CellToolError, match="不可达"):
        offline.execute("t", {})


# ------------------------------------------------------------------ llm


def _gateway(handler) -> TokenGatewayLlmClient:
    return TokenGatewayLlmClient(
        base_url="https://token.example",
        api_key="gw-key",
        model="test-model",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_llm_client_request_shape_and_tool_calls_parse() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "function": {
                                        "name": "dashboard_spec_create",
                                        "arguments": json.dumps(
                                            {"title": "GDP 对比", "spec": {"indicators": ["IND-GDP"]}},
                                            ensure_ascii=False,
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    client = _gateway(handler)
    message = client.complete(
        [{"role": "user", "content": "建个仪表盘"}], tools=TOOL_SCHEMAS
    )
    assert seen["url"] == "https://token.example/v1/chat/completions"
    assert seen["auth"] == "Bearer gw-key"
    assert seen["body"]["model"] == "test-model"
    assert seen["body"]["tools"] == TOOL_SCHEMAS
    assert message.has_tool_calls
    call = message.tool_calls[0]
    assert call.id == "call-1"
    assert call.name == "dashboard_spec_create"
    assert call.arguments == {"title": "GDP 对比", "spec": {"indicators": ["IND-GDP"]}}


def test_llm_client_plain_answer() -> None:
    client = _gateway(
        lambda r: httpx.Response(
            200, json={"choices": [{"message": {"content": "已建好"}}]}
        )
    )
    message = client.complete([{"role": "user", "content": "hi"}], tools=[])
    assert message.text() == "已建好"
    assert not message.has_tool_calls


@pytest.mark.parametrize(
    "handler, pattern",
    [
        (lambda r: httpx.Response(503, text="down"), "返回 503"),
        (
            lambda r: httpx.Response(200, json={"unexpected": True}),
            "不合 OpenAI 契约",
        ),
        (
            lambda r: httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "c",
                                        "function": {
                                            "name": "t",
                                            "arguments": "not-json",
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            ),
            "tool_calls",
        ),
    ],
)
def test_llm_client_errors(handler, pattern) -> None:
    with pytest.raises(LlmGatewayError, match=pattern):
        _gateway(handler).complete([{"role": "user", "content": "hi"}], tools=[])


def test_llm_client_unreachable() -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    with pytest.raises(LlmGatewayError, match="不可达"):
        _gateway(unreachable).complete([{"role": "user", "content": "hi"}], tools=[])


# ------------------------------------------------------------------ tools


def test_tool_schemas_are_the_five_artifact_kinds() -> None:
    names = {t["function"]["name"] for t in TOOL_SCHEMAS}
    assert names == set(ARTIFACT_TOOL_KINDS)
    assert names == {
        "dashboard_spec_create",
        "composite_indicator_create",
        "research_report_create",
        "alert_rule_create",
        "refresh_schedule_create",
    }
    for schema in TOOL_SCHEMAS:
        params = schema["function"]["parameters"]
        assert set(params["required"]) == {"title", "spec"}
        assert params["properties"]["spec"]["type"] == "object"


def test_dashboard_tool_spec_description_embeds_v1_contract() -> None:
    """dashboard_spec_create 的 spec 描述内嵌 v1 结构示例（快照口径，
    wire-dashboard-share 任务 1.4：AI 新仪表盘默认可渲染可分享）。"""
    dashboard = next(
        t for t in TOOL_SCHEMAS if t["function"]["name"] == "dashboard_spec_create"
    )
    desc = dashboard["function"]["parameters"]["properties"]["spec"]["description"]
    # v1 契约要素在场：panels/type 四值/span/snapshot(含 columns+rows)/config
    for token in (
        "panels",
        "kpi|line|bar|table",
        '"span"',
        "snapshot",
        "columns",
        "rows",
        "config",
        "unit",
        "subtitle",
    ):
        assert token in desc, token
    # 强调取到的数据写进 snapshot（不放占位符）
    assert "写进 snapshot" in desc
    # source 标注提示（wire-dashboard-refresh 任务 1.3：刷新依赖此标注取数）
    assert "刷新" in desc
    assert '"source"' in desc
    # 其余四类工具的 spec 描述不受影响（仍为各自的概要文案）
    composite = next(
        t for t in TOOL_SCHEMAS if t["function"]["name"] == "composite_indicator_create"
    )
    assert "panels" not in composite["function"]["parameters"]["properties"]["spec"]["description"]


@pytest.mark.parametrize(
    "args, expect_error",
    [
        ({"title": "T", "spec": {"a": 1}}, None),
        ({"spec": {"a": 1}}, "缺少非空 title"),
        ({"title": "  ", "spec": {}}, "缺少非空 title"),
        ({"title": "T"}, "缺少非空 spec"),
        ({"title": "T", "spec": "文本不是对象"}, "缺少非空 spec"),
        ("不是对象", "必须是 JSON 对象"),
    ],
)
def test_validate_artifact_args(args, expect_error) -> None:
    error = validate_artifact_args("dashboard_spec_create", args)
    if expect_error is None:
        assert error is None
    else:
        assert error is not None and expect_error in error


def test_stage_artifact_roundtrip() -> None:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    AiBase.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()

    result = stage_artifact(
        db,
        conversation_id=1,
        customer_id="cust-001",
        tool_name="refresh_schedule_create",
        args={"title": "每日 9 点刷新", "spec": {"cron": "0 9 * * *", "tz": "Asia/Shanghai"}},
    )
    assert result["ok"] is True
    assert result["kind"] == "refresh_schedule"
    assert result["status"] == "draft"
    row = db.get(AIDraftArtifact, result["artifact_id"])
    assert row.kind == "refresh_schedule"
    assert row.customer_id == "cust-001"
    assert row.spec["title"] == "每日 9 点刷新"
    assert row.spec["spec"]["tz"] == "Asia/Shanghai"
    assert row.status == "draft"

    # 不合法入参：拒绝暂存（不留半成品），表里不多一行
    bad = stage_artifact(
        db, conversation_id=1, customer_id="cust-001",
        tool_name="dashboard_spec_create", args={"title": ""},
    )
    assert bad["ok"] is False and "title" in bad["error"]
    assert len(db.query(AIDraftArtifact).all()) == 1


# ------------------------------------------------------------------ 迁移链


@pytest.fixture
def migrated_sqlite(tmp_path, monkeypatch):
    """对临时库跑 alembic upgrade head（当前链 0001→0006），返回连接串。"""
    db_url = f"sqlite:///{tmp_path}/migration.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "alembic"))
    command.upgrade(cfg, "head")
    return db_url


def test_migration_0005_creates_ai_tables(migrated_sqlite) -> None:
    engine = create_engine(migrated_sqlite)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert {
        "ai_conversations",
        "ai_messages",
        "ai_draft_artifacts",
        "alembic_version",
    } <= tables

    assert {c["name"] for c in inspector.get_columns("ai_conversations")} == {
        "id", "customer_id", "title", "created_at",
    }
    assert {c["name"] for c in inspector.get_columns("ai_messages")} == {
        "id", "conversation_id", "role", "content", "created_at",
    }
    assert {c["name"] for c in inspector.get_columns("ai_draft_artifacts")} == {
        "id", "conversation_id", "customer_id", "kind", "spec", "status", "created_at",
    }
    with engine.connect() as conn:
        version = conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
    # 迁移链只增不改：head 随链推进（wire-rich-text-docs = 0038 草稿层表）
    assert version == "0047_dataset_acl"


def _cfg() -> Config:
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "alembic"))
    return cfg


def test_migration_0005_downgrade_drops_ai_tables(migrated_sqlite, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", migrated_sqlite)
    command.downgrade(_cfg(), "0004_billing")
    inspector = inspect(create_engine(migrated_sqlite))
    tables = set(inspector.get_table_names())
    assert "ai_conversations" not in tables
    assert "ai_messages" not in tables
    assert "ai_draft_artifacts" not in tables
    assert "billing_plans" in tables  # 前置迁移不受影响


# ------------------------------------- v2 生成侧指令与结构闸（wire-chart-contract-v2）


def test_dashboard_tool_description_covers_v2_contract() -> None:
    """工具描述包含 v2 各要素（类型谱/编码/轴类型/窗格/叠加层/逃生舱边界）。"""
    dashboard = next(
        item
        for item in TOOL_SCHEMAS
        if item["function"]["name"] == "dashboard_spec_create"
    )
    # v2 契约写在 spec 参数的描述里（外层是五类产物共用的通用文案）
    description = dashboard["function"]["parameters"]["properties"]["spec"]["description"]
    for needle in [
        "仪表盘规格 v2",
        "candlestick",
        "encoding",
        "time",
        "panes",
        "overlays",
        "markLine",
        "raw_option",
        "不可序列化",
        "纯 JSON",
    ]:
        assert needle in description, f"工具描述缺少 v2 要素：{needle}"


def test_validate_artifact_args_rejects_bad_panel_type() -> None:
    """面板类型不在类型谱内 → 落库前拒绝并列出可选类型。"""
    error = validate_artifact_args(
        "dashboard_spec_create",
        {
            "title": "看板",
            "spec": {"panels": [{"id": "p1", "type": "waterfall"}]},
        },
    )
    assert error is not None
    assert "waterfall" in error
    assert "candlestick" in error  # 列出可选类型


def test_validate_artifact_args_accepts_advanced_types() -> None:
    """高级档十类全部放行（不误伤合法生成）。"""
    panels = [
        {"id": f"p{i}", "type": t}
        for i, t in enumerate(
            [
                "candlestick",
                "scatter",
                "pie",
                "heatmap",
                "gauge",
                "radar",
                "treemap",
                "sankey",
                "funnel",
                "boxplot",
            ]
        )
    ]
    assert (
        validate_artifact_args(
            "dashboard_spec_create", {"title": "看板", "spec": {"panels": panels}}
        )
        is None
    )


def test_validate_artifact_args_rejects_function_in_raw_option() -> None:
    """raw_option 里塞函数 → 拒绝（落库会渲染成坏面板）。"""
    error = validate_artifact_args(
        "dashboard_spec_create",
        {
            "title": "看板",
            "spec": {
                "panels": [
                    {
                        "id": "p1",
                        "type": "line",
                        "raw_option": {"tooltip": {"formatter": lambda v: v}},
                    }
                ]
            },
        },
    )
    assert error is not None
    assert "不可序列化" in error


def test_validate_artifact_args_accepts_string_formatter() -> None:
    """字符串形式的 formatter 合法（成品看板实态：JSON 里就是字符串）。"""
    assert (
        validate_artifact_args(
            "dashboard_spec_create",
            {
                "title": "看板",
                "spec": {
                    "panels": [
                        {
                            "id": "p1",
                            "type": "line",
                            "raw_option": {
                                "tooltip": {"valueFormatter": "(v) => v"},
                                "series": [{"type": "line", "data": [1, 2]}],
                            },
                        }
                    ]
                },
            },
        )
        is None
    )


def test_validate_artifact_args_keeps_v1_panels_passing() -> None:
    """存量 v1 形状零漂移：无 raw_option、四类基础面板照常通过。"""
    assert (
        validate_artifact_args(
            "dashboard_spec_create",
            {
                "title": "宏观",
                "spec": {
                    "panels": [
                        {"id": "k1", "type": "kpi", "snapshot": {"columns": ["v"], "rows": [[1]]}},
                        {"id": "l1", "type": "line", "snapshot": {"columns": ["x", "y"], "rows": []}},
                        {"id": "b1", "type": "bar"},
                        {"id": "t1", "type": "table"},
                    ]
                },
            },
        )
        is None
    )


def test_validate_artifact_args_accepts_list_panel_with_title_role() -> None:
    """list 面板（wire-list-panel）：声明 title 角色即通过（新类型在白名单内）。"""
    assert (
        validate_artifact_args(
            "dashboard_spec_create",
            {
                "title": "资讯流",
                "spec": {
                    "panels": [
                        {
                            "id": "f1",
                            "type": "list",
                            "encoding": {"title": "title", "image": "image_url"},
                            "snapshot": {
                                "columns": ["title", "image_url"],
                                "rows": [["标题", "https://example.com/a.jpg"]],
                            },
                        }
                    ]
                },
            },
        )
        is None
    )


def test_validate_artifact_args_rejects_list_panel_without_title_role() -> None:
    """list 面板缺 title 角色 → 生成侧先拦（前端会整面板判坏）。"""
    error = validate_artifact_args(
        "dashboard_spec_create",
        {
            "title": "资讯流",
            "spec": {
                "panels": [
                    {
                        "id": "f1",
                        "type": "list",
                        "encoding": {"summary": "summary"},
                        "snapshot": {"columns": ["summary"], "rows": [["摘要"]]},
                    }
                ]
            },
        },
    )
    assert error is not None
    assert "encoding.title" in error
