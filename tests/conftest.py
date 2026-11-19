"""测试夹具：离线 SQLite fixture + 真实 Postgres 内核（事件层约束不可 mock）。

- fd-wire 侧是**构造出来的最小 SQLite**（``db_path``）：只建适配器读到的列，
  不依赖 live 库、不联网。
- 内核侧必须有真实 Postgres：只追加 / 定型 / 幂等由 DB 触发器与唯一约束保证。
  ``ERP_LEDGER_DATABASE_URL`` 未设置 → 需要 DB 的用例 skip（与
  ``fd-erp-ledger-mcp/tests/conftest.py`` 同风格）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
#: 内核是兄弟仓；本仓单独检出 / CI 单仓 checkout 时不存在。
KERNEL_ROOT = Path(
    os.environ.get("ERP_LEDGER_KERNEL_ROOT") or REPO_ROOT.parent / "fd-erp-ledger-mcp"
)
if str(KERNEL_ROOT) not in sys.path:
    sys.path.insert(0, str(KERNEL_ROOT))

try:
    from fd_erp_ledger_mcp.config import Settings  # noqa: E402

    KERNEL_AVAILABLE = True
    KERNEL_IMPORT_ERROR: str | None = None
except ModuleNotFoundError as exc:  # 只有「缺内核」走这条；内核内部 ImportError 照旧炸
    if not str(exc).startswith("No module named 'fd_erp_ledger_mcp"):
        raise
    Settings = None  # type: ignore[assignment]
    KERNEL_AVAILABLE = False
    KERNEL_IMPORT_ERROR = f"{exc}（KERNEL_ROOT={KERNEL_ROOT}）"

TENANT = "fd-wire-e2e"
EVENT_TYPE = "marketing.content.published"


# ─────────────────────────────────────────────── fd-wire 离线 fixture


def _spec(title: str, slug: str | None) -> str:
    """fd-wire 的 spec 信封：title 在外层，自定义位在 ``spec.spec``。"""
    inner: dict[str, object] = {}
    if slug:
        inner["showcase_slug"] = slug
    return json.dumps({"title": title, "spec": inner}, ensure_ascii=False)


def build_fixture_db(path: Path) -> None:
    """最小列集：只建适配器真正读到的列。

    列集与真实库对齐（``artifact_versions`` 没有 status/published_at——见迁移 0021）：
    发布状态与时间戳在 ``artifacts`` 上，版本锚点是 ``published_version``。
    """
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE artifacts (
                id INTEGER PRIMARY KEY,
                customer_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                version INTEGER NOT NULL,
                status_history TEXT NOT NULL DEFAULT '[]',
                visibility TEXT NOT NULL DEFAULT 'private',
                access_mode TEXT NOT NULL DEFAULT 'open',
                published_at TEXT,
                published_version INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE artifact_versions (
                id INTEGER PRIMARY KEY,
                artifact_id INTEGER NOT NULL,
                version INTEGER NOT NULL,
                spec TEXT NOT NULL,
                operator TEXT,
                source_ref TEXT,
                created_at TEXT NOT NULL,
                UNIQUE (artifact_id, version)
            );
            """
        )
        now = "2026-11-12 00:00:00"
        conn.executemany(
            "INSERT INTO artifacts (id, customer_id, kind, title, status, version, "
            "visibility, published_at, published_version, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                # ① 已发布：对外可见，且发布锚点是 v1（之后又编辑出 v2，但 v2 未发布）
                (1, "acme", "research_report", "行业月报（已发布）", "published", 2,
                 "market", "2026-11-01 08:00:00", 1, now, now),
                # ② 发布后回滚：状态已不是 published，但 published_at 仍在（归档/恢复不清时间戳）
                (2, "acme", "research_report", "已回滚的内容", "draft", 1,
                 "market", "2026-11-02 08:00:00", 1, now, now),
                # ③ 从未发布：无 published_at
                (3, "acme", "research_report", "从未发布的内容", "draft", 1,
                 "private", None, None, now, now),
            ],
        )
        conn.executemany(
            "INSERT INTO artifact_versions (artifact_id, version, spec, operator, "
            "source_ref, created_at) VALUES (?,?,?,?,?,?)",
            [
                (1, 1, _spec("行业月报（已发布）", "industry-monthly"),
                 "console:acme", None, now),
                (1, 2, _spec("行业月报（已发布）", "industry-monthly"),
                 "console:acme", None, now),
                (2, 1, _spec("已回滚的内容", "rolled-back"),
                 "console:acme", None, now),
                (3, 1, _spec("从未发布的内容", "never-published"),
                 "console:acme", None, now),
            ],
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "fd_wire_fixture.db"
    build_fixture_db(path)
    return path


# ─────────────────────────────────────────────── 内核（真实 Postgres）


def _require_migrated(dsn: str) -> None:
    """没有 psycopg-pool（无法 migrate）时的退路：库已迁移到 0011 才能跑。"""
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        ok = conn.execute(
            """
            SELECT to_regclass('public.business_events') IS NOT NULL
               AND to_regclass('public.event_types') IS NOT NULL
               AND EXISTS (SELECT 1 FROM information_schema.columns
                            WHERE table_name = 'event_types'
                              AND column_name = 'producer')
            """
        ).fetchone()[0]
    if not ok:
        pytest.skip(
            "内核测试库未迁移到 0011（缺 event_types.producer / 业务表）；"
            "用装有 psycopg-pool 的解释器跑（会调用内核 migrate），或先执行内核迁移"
        )


def _ensure_event_type(settings: "Settings") -> None:
    """类型未登记时补一条（0011 已登记则为 no-op）。

    注册是内核迁移的职责；这里只在旧库上兜底，让适配器测试不被迁移进度卡住。
    新列（producer/domain/evidence_ref）按库的实际列集填——迁移落位后自动带上。
    """
    import psycopg

    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        present = conn.execute(
            "SELECT 1 FROM event_types WHERE event_type = %s", (EVENT_TYPE,)
        ).fetchone()
        if present:
            return
        columns = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'event_types'"
            ).fetchall()
        }
        desired = {
            "event_type": EVENT_TYPE,
            "description": "内容发布（营销：fd-wire 中已发布的 artifact 版本行）",
            "payload_schema": json.dumps(
                {
                    "required": ["slug", "title", "published_at", "artifact_ref"],
                    "properties": ["slug", "title", "published_at", "artifact_ref"],
                },
                ensure_ascii=False,
            ),
            "finality_granularity": "day",
            "revision_window": "0 seconds",
            "requires_posting": False,
            "producer": "marketing",
            "domain": "marketing",
            "evidence_ref": "fd-wire/api/fd_wire_api/artifacts/models.py#artifact_versions",
        }
        values = {key: value for key, value in desired.items() if key in columns}
        keys = list(values)
        conn.execute(
            f"INSERT INTO event_types ({', '.join(keys)}) "
            f"VALUES ({', '.join(['%s'] * len(keys))})",
            [values[key] for key in keys],
        )


@pytest.fixture(scope="session")
def ledger_settings():
    """真实内核库连接配置；缺 DSN / 缺内核仓 → skip（不是 fail）。"""
    if not KERNEL_AVAILABLE:
        pytest.skip(f"内核仓不在位：{KERNEL_IMPORT_ERROR}")
    dsn = os.environ.get("ERP_LEDGER_DATABASE_URL")
    if not dsn:
        pytest.skip("ERP_LEDGER_DATABASE_URL 未设置（需真实 Postgres）")
    settings = Settings(database_url=dsn, tenant_id=TENANT)
    try:
        from fd_erp_ledger_mcp.db import migrate

        migrate(settings)  # 一次性、可重放（内核自己也是这么做的）
    except ModuleNotFoundError as exc:
        if "psycopg_pool" not in str(exc):
            raise
        _require_migrated(dsn)
    return settings


@pytest.fixture()
def ledger(ledger_settings):
    """内核库已就绪 + 类型已登记。

    不清空 ``business_events``：只追加表的清空要走维护开关 + TRUNCATE CASCADE，
    而 ``ERP_LEDGER_DATABASE_URL`` 指向的测试库可能同时被别的会话使用（多会话
    并发写同仓协定）。用例各用唯一租户隔离，谁也不踩谁。
    """
    _ensure_event_type(ledger_settings)
    return ledger_settings
