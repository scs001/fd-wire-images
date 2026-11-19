"""已发布 artifact → 内核事件：适配器的三条机器判据
（change: fuse-marketing-onto-event-kernel 2.1 / 2.2 / 2.3）。

断言口径：
- 首跑：只有一个「仍 published 且 published_at 非空」的**发布版本**被写成事实；
  发布锚点之后编辑出的新版本（v2）不产生事实。
- 重跑：0 新增，幂等命中报 duplicate；事件总数不变。
- 回滚：published_at 仍在但状态已不是 published 的那条**不写**；已写事实不删不改。

隔离：每个用例一个随机租户（``(tenant, lane, stable_id)`` 是内核幂等键空间），
不 TRUNCATE 共享测试库——别的会话可能正在用同一个库。
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
ADAPTER_PATH = REPO_ROOT / "scripts" / "publish_facts_to_kernel.py"


def _load_adapter():
    spec = importlib.util.spec_from_file_location("publish_facts_to_kernel", ADAPTER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclass 解析注解时要能在 sys.modules 里找到本模块，先登记再执行。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


adapter = _load_adapter()


def _tenant() -> str:
    return f"fd-wire-test-{uuid.uuid4().hex[:8]}"


def _events(settings: Any, tenant: str) -> list[dict[str, Any]]:
    """读回该租户下本适配器写的事件（内核读取面，不直查表）。"""
    import psycopg

    from fd_erp_ledger_mcp import events

    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        result = events.query_events(
            conn, tenant_id=tenant, event_type=adapter.EVENT_TYPE
        )
    return result["items"]


def test_first_run_writes_only_the_published_version(db_path, ledger) -> None:
    tenant = _tenant()
    summary = adapter.run(db_path, settings=ledger, tenant_id=tenant)

    assert summary["written"] == 1
    assert summary["duplicate"] == 0
    assert summary["skipped"] == 2  # 回滚那条 + 从未发布那条

    items = _events(ledger, tenant)
    assert len(items) == 1
    event = items[0]
    assert event["event_type"] == "marketing.content.published"
    assert event["lane"] == "ingest"
    assert event["status"] == "accepted"  # ingest 车道写入即事实
    assert event["stable_id"] == "1:v1"
    assert event["reversal_of"] is None
    # producer 由类型注册表推断（内核 0011 之后才出现在读取面）
    if "producer" in event:
        assert event["producer"] == "marketing"

    payload = event["payload"]
    assert payload["slug"] == "industry-monthly"
    assert payload["title"] == "行业月报（已发布）"
    assert payload["published_at"] == "2026-11-01T08:00:00Z"
    assert payload["artifact_ref"] == "fd-wire:artifact:1:v1"
    assert payload["version"] == 1

    # 发布锚点之后编辑出的 v2 不是对外可见版本，不得凭空成为事实
    assert not any(item["stable_id"] == "1:v2" for item in items)


def test_rerun_is_idempotent(db_path, ledger) -> None:
    tenant = _tenant()
    first = adapter.run(db_path, settings=ledger, tenant_id=tenant)
    second = adapter.run(db_path, settings=ledger, tenant_id=tenant)

    assert first["written"] == 1
    assert second["written"] == 0
    assert second["duplicate"] == 1
    assert len(_events(ledger, tenant)) == 1


def test_rolled_back_version_writes_nothing(db_path, ledger) -> None:
    tenant = _tenant()
    summary = adapter.run(db_path, settings=ledger, tenant_id=tenant)

    stable_ids = [item["stable_id"] for item in _events(ledger, tenant)]
    assert stable_ids == ["1:v1"]
    assert not any(sid.startswith("2:") for sid in stable_ids)
    assert not any(sid.startswith("3:") for sid in stable_ids)

    skipped = {
        outcome.artifact_id: outcome
        for outcome in summary["outcomes"]
        if outcome.kind == "skipped"
    }
    assert 2 in skipped and "published" in skipped[2].detail
    assert 3 in skipped  # 从未发布：状态也不是 published


def test_published_status_without_published_at_is_skipped(db_path) -> None:
    """状态已是 published 但 published_at 为空（未对外可见）→ 不写、报告跳过。"""
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO artifacts (id, customer_id, kind, title, status, version, "
            "visibility, published_at, published_version, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (4, "acme", "research_report", "状态已发但未对外可见", "published", 1,
             "private", None, None, "2026-11-12 00:00:00", "2026-11-12 00:00:00"),
        )
        conn.execute(
            "INSERT INTO artifact_versions (artifact_id, version, spec, operator, "
            "source_ref, created_at) VALUES (?,?,?,?,?,?)",
            (4, 1, "{\"title\":\"状态已发但未对外可见\",\"spec\":{\"showcase_slug\":\"hidden\"}}",
             "console:acme", None, "2026-11-12 00:00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    facts, skipped = adapter.collect(db_path)
    assert [fact.stable_id for fact in facts] == ["1:v1"]
    hidden = [item for item in skipped if item.artifact_id == 4]
    assert hidden and "published_at" in hidden[0].reason


def test_dry_run_prints_without_writing_or_needing_dsn(db_path) -> None:
    """dry-run 不建内核连接：没设 DSN 也能跑（本用例不请求 ledger fixture）。"""
    summary = adapter.run(db_path, dry_run=True)

    assert summary["planned"] == 1
    assert summary["written"] == 0
    assert summary["duplicate"] == 0
    assert summary["skipped"] == 2
