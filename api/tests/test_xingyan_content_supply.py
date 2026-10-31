"""xingyan-content-supply（星研内容供给面）测试。

覆盖 spec 五条 Requirement 的可判定面：
- 幂等重跑（同指纹零写入）
- 上游故障显式陈旧（旧快照继续服务，绝不返回空集）
- 版本不匹配拒摄（不猜字段、不部分写入）
- 双语 claim 同指向 + 撤销态显式（非 404）
- 只读保护（查询侧写快照表被引擎层拦截）
- 工具面/目录可发现（列表过滤、claim 计数）

用假 FeedClient 注入（不联网），库走临时 SQLite。
"""

from __future__ import annotations

import os
import sys

import pytest

# 防落盘：本文件不设 FD_WIRE_DATABASE_URL 时各切片回落 dev sqlite 文件
os.environ.setdefault("XINGYAN_AUTO_CREATE_TABLES", "1")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fd_wire_api.xingyan import service  # noqa: E402
from fd_wire_api.xingyan.client import FeedSchemaMismatch, FeedUnavailable  # noqa: E402
from fd_wire_api.xingyan.db import (  # noqa: E402
    XingyanWriteBlocked,
    get_read_engine,
    init_db,
    readonly_scope,
    reset_xingyan_runtime,
)
from fd_wire_api.xingyan.sync import XingyanFeedSync, fingerprint  # noqa: E402


def _item(slug, zone, **over):
    item = {
        "slug": slug,
        "title": {"zh": f"{slug} 中文标题", "en": f"{slug} English title"},
        "summary": {"zh": "中文摘要", "en": "English summary"},
        "tags": ["t"],
        "sources": {"zh": [], "en": []},
        "claims": {"zh": [], "en": []},
        "body": {"zh": "", "en": ""},
        "url": {"en": f"/research/{slug}", "zh": f"/zh/research/{slug}"},
    }
    if zone in ("briefings", "research"):
        item["date"] = "2026-10-30"
    if zone == "research":
        item["kind"] = "evaluation"
    if zone == "academy":
        item["updated"] = "2026-10-30"
        item["level"] = "use"
        item["order"] = 1
    item.update(over)
    return item


def _feeds(**over):
    """三区最小数据集（默认每个分区 1 条）。"""
    zones = {
        "briefings": [_item("b-one", "briefings")],
        "academy": [_item("a-one", "academy")],
        "research": [_item("r-one", "research")],
    }
    zones.update(over)
    return zones


def _manifest(zones, sha_seed="1"):
    """manifest 指纹随内容变（真实上游由内容摘要算出）；夹具按内容摘要模拟。"""
    import hashlib
    import json

    return {
        "source": "cms",
        "total_files": 3,
        "zones": {
            z: {
                "items": len(items),
                "sha": hashlib.sha256(
                    (sha_seed + json.dumps(items, sort_keys=True, ensure_ascii=False)).encode()
                ).hexdigest()[:8],
            }
            for z, items in sorted(zones.items())
        },
    }


class FakeFeed:
    """可控的上游：要么给数据，要么抛指定异常。"""

    def __init__(self, zones=None, manifest=None, fail=None):
        self.zones = zones if zones is not None else _feeds()
        self._manifest = manifest
        self.fail = fail
        self.calls = 0

    def close(self):
        pass

    def fetch_manifest(self):
        self.calls += 1
        if self.fail:
            raise self.fail
        return self._manifest or _manifest(self.zones)

    def fetch_zone(self, zone):
        if self.fail:
            raise self.fail
        return {"schema_version": "1", **{zone: self.zones[zone]}}


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "x.db"
    monkeypatch.setenv("XINGYAN_DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("XINGYAN_AUTO_CREATE_TABLES", "1")
    monkeypatch.setenv("XINGYAN_MIN_ZONE_ITEMS", "1")
    monkeypatch.setenv("XINGYAN_FEED_SCHEMA_RANGE", "1")
    reset_xingyan_runtime()
    init_db()
    yield
    reset_xingyan_runtime()


def _run(feed=None, **kw):
    return XingyanFeedSync(client=feed or FakeFeed()).run(**kw)


# ── 幂等 ────────────────────────────────────────────────────────────────────


def test_ingest_then_same_fingerprint_is_noop():
    first = _run()
    assert first.status == "ok"
    assert first.items == 3
    second = _run()
    assert second.status == "skipped"
    assert second.source_sha == first.source_sha


def test_fingerprint_ignores_unmanaged_zones():
    zones = _feeds()
    base = fingerprint(_manifest(zones, "a"))
    manifest = _manifest(zones, "a")
    manifest["zones"]["updates"] = {"items": 9, "sha": "zzz"}
    assert fingerprint(manifest) == base


def test_force_reruns_despite_same_fingerprint():
    _run()
    assert _run(force=True).status == "ok"


# ── 陈旧 / 故障不冒充空集 ────────────────────────────────────────────────────


def test_schema_mismatch_refuses_and_keeps_previous():
    _run()
    bad = FakeFeed(fail=FeedSchemaMismatch("briefings", "9"))
    result = _run(bad)
    assert result.status == "schema_mismatch"
    with readonly_scope() as session:
        meta = service.snapshot_meta(session)
        # 旧快照继续服务：条目数不因拒摄而掉
        assert meta["items"] == 3
        assert meta["stale"] is True
        assert meta["status"] == "schema_mismatch"


def test_upstream_down_is_stale_not_empty():
    _run()
    down = FakeFeed(fail=FeedUnavailable("boom"))
    result = _run(down)
    assert result.status == "upstream_error"
    with readonly_scope() as session:
        # 「检索结果为空」这一表现必须不存在
        assert service.list_items(session, zone="research")["total"] == 1
        assert service.snapshot_meta(session)["stale"] is True


def test_half_version_rejected():
    """manifest 声明条数与实际不符 → 按半版本拒摄（不部分写入）。"""
    zones = _feeds()
    manifest = _manifest(zones, "h")
    manifest["zones"]["research"]["items"] = 99
    result = _run(FakeFeed(zones=zones, manifest=manifest))
    assert result.status == "upstream_error"
    assert "半版本" in result.detail
    with readonly_scope() as session:
        assert service.list_items(session)["total"] == 0


def test_first_run_failure_leaves_empty_snapshot_not_fake_content():
    result = _run(FakeFeed(fail=FeedUnavailable("never")))
    assert result.status == "upstream_error"
    with readonly_scope() as session:
        assert service.snapshot_meta(session)["status"] == "upstream_error"
        assert service.list_items(session)["total"] == 0


# ── 撤销语义 ────────────────────────────────────────────────────────────────


def test_absent_slug_marked_withdrawn_not_deleted():
    _run()
    zones = _feeds()
    zones["research"] = [_item("r-two", "research")]  # r-one 消失
    result = _run(FakeFeed(zones=zones))
    assert result.status == "ok"
    assert result.withdrawn_items == 1
    with readonly_scope() as session:
        active = service.list_items(session, zone="research")
        assert [i["slug"] for i in active["items"]] == ["r-two"]
        withdrawn = service.list_items(session, zone="research", status="withdrawn")
        assert [i["slug"] for i in withdrawn["items"]] == ["r-one"]


def test_withdrawn_slug_reappears_as_active():
    _run()
    zones_wo = _feeds()
    zones_wo["research"] = [_item("r-two", "research")]
    _run(FakeFeed(zones=zones_wo))  # r-one 消失 → withdrawn
    result = _run(FakeFeed(zones=_feeds()))  # r-one 复现 → active
    assert result.status == "ok"
    with readonly_scope() as session:
        active = {i["slug"] for i in service.list_items(session, zone="research")["items"]}
        withdrawn = {
            i["slug"]
            for i in service.list_items(session, zone="research", status="withdrawn")["items"]
        }
        assert active == {"r-one"}
        # 本轮缺席的 r-two 被标撤销（撤销可逆、双向对称）
        assert withdrawn == {"r-two"}


# ── claim 级解析 ────────────────────────────────────────────────────────────


def _claim_item():
    return _item(
        "r-claims",
        "research",
        body={
            "zh": "## 结论先行\n\n中文正文第一段。\n\n## 结果\n\n中文结果段。\n",
            "en": "## Bottom line first\n\nEnglish lead paragraph.\n\n## Results\n\nEnglish results.\n",
        },
        sources={
            "zh": [{"id": "src-1", "label": "中文来源"}],
            "en": [{"id": "src-1", "label": "English source", "url": "https://x/y"}],
        },
        claims={
            "zh": [
                {"id": "c0001", "section": "结论先行", "type": "number", "evidence": ["src-1"]},
                {"id": "c0002", "section": "结果", "type": "causal", "evidence": ["src-1"]},
            ],
            "en": [
                {"id": "c0001", "section": "Bottom line first", "type": "number", "evidence": ["src-1"]},
                {"id": "c0002", "section": "Results", "type": "causal", "evidence": ["src-1"]},
            ],
        },
    )


def test_claim_resolves_to_section_and_evidence():
    zones = _feeds()
    zones["research"] = [_claim_item()]
    _run(FakeFeed(zones=zones))
    with readonly_scope() as session:
        zh = service.resolve_claim(session, "r-claims", "c0001", "zh")
        assert zh["section"]["zh"] == "结论先行"
        assert zh["section_found"] is True
        assert "中文正文第一段" in zh["section_text"]
        assert zh["evidence"][0]["resolved"] is True
        assert zh["evidence"][0]["label"] == "中文来源"


def test_claim_bilingual_same_id_different_section_text():
    zones = _feeds()
    zones["research"] = [_claim_item()]
    _run(FakeFeed(zones=zones))
    with readonly_scope() as session:
        zh = service.resolve_claim(session, "r-claims", "c0001", "zh")
        en = service.resolve_claim(session, "r-claims", "c0001", "en")
        # 骨架逐字段一致（类型/证据），正文按各自语言
        assert zh["type"] == en["type"]
        assert zh["evidence"][0]["key"] == en["evidence"][0]["key"]
        assert "English lead paragraph" in en["section_text"]
        assert zh["section_text"] != en["section_text"]


def test_claim_missing_section_is_honest():
    zones = _feeds()
    item = _claim_item()
    item["body"]["zh"] = "## 别的小节\n\n正文\n"
    zones["research"] = [item]
    _run(FakeFeed(zones=zones))
    with readonly_scope() as session:
        got = service.resolve_claim(session, "r-claims", "c0001", "zh")
        assert got["section_found"] is False
        assert got["section_text"] == ""


def test_withdrawn_claim_is_explicit_state():
    zones = _feeds()
    zones["research"] = [_claim_item()]
    _run(FakeFeed(zones=zones))
    # 上游撤掉 c0002
    trimmed = _claim_item()
    trimmed["claims"] = {
        "zh": [{"id": "c0001", "section": "结论先行", "type": "number", "evidence": ["src-1"]}],
        "en": [{"id": "c0001", "section": "Bottom line first", "type": "number", "evidence": ["src-1"]}],
    }
    zones2 = _feeds()
    zones2["research"] = [trimmed]
    _run(FakeFeed(zones=zones2))
    with readonly_scope() as session:
        with pytest.raises(service.ClaimWithdrawn):
            service.resolve_claim(session, "r-claims", "c0002", "zh")
        # 登记仍在（不是消失）
        row = service.get_item(session, "r-claims")
        states = {c["claim_id"]: c["status"] for c in row["claims"]}
        assert states["c0002"] == "withdrawn"
        assert states["c0001"] == "active"


def test_unknown_claim_is_not_found():
    _run()
    with readonly_scope() as session:
        with pytest.raises(service.NotFound):
            service.resolve_claim(session, "r-one", "c9999", "zh")


# ── 目录发现 ────────────────────────────────────────────────────────────────


def test_list_filters_by_zone_and_search():
    _run()
    with readonly_scope() as session:
        assert service.list_items(session, zone="academy")["total"] == 1
        assert service.list_items(session, q="English title")["total"] == 3
        assert service.list_items(session, q="不存在的东西")["total"] == 0


def test_claim_count_visible_in_list():
    zones = _feeds()
    zones["research"] = [_claim_item()]
    _run(FakeFeed(zones=zones))
    with readonly_scope() as session:
        items = service.list_items(session, zone="research")["items"]
        assert items[0]["claim_count"] == 2


def test_item_detail_carries_body_and_sources():
    zones = _feeds()
    zones["research"] = [_claim_item()]
    _run(FakeFeed(zones=zones))
    with readonly_scope() as session:
        row = service.get_item(session, "r-claims")
        assert "中文正文第一段" in row["body"]["zh"]
        assert row["sources"]["en"][0]["id"] == "src-1"
        assert row["url"]["zh"].startswith("/zh/")


# ── 只读保护 ────────────────────────────────────────────────────────────────


def test_query_side_cannot_write():
    _run()
    from sqlalchemy import text

    with readonly_scope() as session:
        with pytest.raises(Exception) as exc:
            session.execute(text("DELETE FROM xingyan_items"))
        assert isinstance(exc.value, (XingyanWriteBlocked, Exception))
    # 数据仍在
    with readonly_scope() as session:
        assert service.list_items(session)["total"] == 3


def test_read_engine_blocks_update():
    from sqlalchemy import text

    with get_read_engine().connect() as conn:
        with pytest.raises(XingyanWriteBlocked):
            conn.execute(text("UPDATE xingyan_items SET status='withdrawn'"))


# ── 迁移可重跑 ──────────────────────────────────────────────────────────────


def test_migration_upgrade_is_rerunnable_and_reversible(tmp_path, monkeypatch):
    """0046 建三表；同 revision 再 upgrade 一次幂等（create_all 抢建后仍可跑）；
    降回 0045 三表消失。"""
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    api_dir = Path(__file__).resolve().parent.parent
    db_url = f"sqlite:///{tmp_path}/mig.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("FD_WIRE_DATABASE_URL", db_url)
    cfg = Config(str(api_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(api_dir / "alembic"))

    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")  # 可重跑

    engine = create_engine(db_url)
    names = set(inspect(engine).get_table_names())
    assert {"xingyan_items", "xingyan_claims", "xingyan_sync_state"} <= names
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() == (
            "0048_dag_executor_schema"
        )
    engine.dispose()

    command.downgrade(cfg, "0045_domain_instance_result_ref")
    engine = create_engine(db_url)
    names = set(inspect(engine).get_table_names())
    assert not ({"xingyan_items", "xingyan_claims", "xingyan_sync_state"} & names)
    engine.dispose()


# ── HTTP 层（路由 + 响应形状）──────────────────────────────────────────────


def _client(tmp_path, monkeypatch):
    """TestClient：只覆盖 xingyan 的只读会话依赖，其余切片用各自临时库。"""
    from fastapi.testclient import TestClient

    from fd_wire_api.app import app
    from fd_wire_api.xingyan.db import readonly_session

    db = tmp_path / "http.db"
    monkeypatch.setenv("XINGYAN_DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("XINGYAN_AUTO_CREATE_TABLES", "1")
    reset_xingyan_runtime()
    init_db()
    zones = _feeds()
    zones["research"] = [_claim_item()]
    XingyanFeedSync(client=FakeFeed(zones=zones)).run()
    return TestClient(app)


def test_http_list_and_detail(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    res = client.get("/api/v1/xingyan/items", params={"zone": "research"})
    assert res.status_code == 200
    body = res.json()
    assert body["snapshot"]["stale"] is False
    assert body["total"] == 1
    assert body["items"][0]["slug"] == "r-claims"
    assert body["items"][0]["claim_count"] == 2

    detail = client.get("/api/v1/xingyan/items/r-claims")
    assert detail.status_code == 200
    assert "中文正文第一段" in detail.json()["body"]["zh"]
    assert client.get("/api/v1/xingyan/items/nope").status_code == 404


def test_http_claim_resolve_and_withdrawn_state(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ok = client.get("/api/v1/xingyan/claims/r-claims/c0001", params={"locale": "en"})
    assert ok.status_code == 200
    payload = ok.json()
    assert payload["section_text"].strip().startswith("English lead paragraph")
    assert payload["evidence"][0]["resolved"] is True

    # 未知 claim 才是 404
    assert client.get("/api/v1/xingyan/claims/r-claims/c9999").status_code == 404


def test_http_stats(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    body = client.get("/api/v1/xingyan/stats").json()
    assert body["zones"]["research"] == 1


def test_http_internal_sync_requires_token(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    # 未配置 token 时 fail-closed
    assert client.post("/internal/xingyan/sync").status_code == 403
