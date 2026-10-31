"""存量归并迁移测试（wire-ux-consolidation 任务 3.4，alembic 0019）。

覆盖：
- 粗归并：五个废除态（evaluating/quoted/awaiting_confirm/building/
  delivery_acceptance）存量单全部 → in_progress，status_history 追加归并
  留痕条目（by=migration），历史条目中的旧态名重映射为 in_progress（连续
  去重）
- delivered / rejected 不动：状态不变、不追加归并事件；历史旧态名同样
  清洗（客户响应无旧态名）
- 新列：contact_email / company 落地（旧行 NULL 起步）
- 幂等：归并函数直接重跑 → 无新事件、无状态再写（全行快照逐字节不变）
- 链头：0019_demand_three_state
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

API_DIR = Path(__file__).resolve().parent.parent
REVISION_PATH = API_DIR / "alembic" / "versions" / "20261010_0019_demand_three_state.py"

LEGACY_STATUSES = (
    "evaluating",
    "quoted",
    "awaiting_confirm",
    "building",
    "delivery_acceptance",
)


def load_revision_module():
    """从迁移文件直接加载模块（拿 merge_legacy_demand_statuses 做幂等重跑）。"""
    spec = importlib.util.spec_from_file_location("wire_0019_revision", REVISION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _history(*statuses: str) -> str:
    """造一条时间线：[{status, at, by, note}]（note 用中性文案，不嵌状态名）。"""
    return json.dumps(
        [
            {"status": s, "at": f"2026-09-01T00:00:0{i}:00+00:00", "by": "seed", "note": "阶段留痕"}
            for i, s in enumerate(statuses)
        ],
        ensure_ascii=False,
    )


def seed_stock_demands(conn) -> dict[str, int]:
    """在 0018 形态的 demands 表里种下各状态存量单，返回 code → id。"""
    plan = {
        # 废除态五例（各自走到对应旧态的时间线）
        "DE-20260901-01": ("evaluating", _history("submitted", "evaluating")),
        "DE-20260901-02": (
            "quoted", _history("submitted", "evaluating", "quoted"),
        ),
        "DE-20260901-03": (
            "awaiting_confirm",
            _history("submitted", "evaluating", "quoted", "awaiting_confirm"),
        ),
        "DE-20260901-04": (
            "building",
            _history(
                "submitted", "evaluating", "quoted", "awaiting_confirm", "building"
            ),
        ),
        "DE-20260901-05": (
            "delivery_acceptance",
            _history(
                "submitted", "evaluating", "quoted", "awaiting_confirm",
                "building", "delivery_acceptance",
            ),
        ),
        # 终态两例：不可动
        "DE-20260901-06": (
            "delivered",
            _history(
                "submitted", "evaluating", "quoted", "awaiting_confirm",
                "building", "delivery_acceptance", "delivered",
            ),
        ),
        "DE-20260901-07": (
            "rejected", _history("submitted", "evaluating", "rejected"),
        ),
        # 现行态一例：本就不含旧态名
        "DE-20260901-08": ("submitted", _history("submitted")),
    }
    ids: dict[str, int] = {}
    for code, (status, history) in plan.items():
        conn.execute(
            sa.text(
                "INSERT INTO demands (code, customer_id, title, intent, status, "
                "status_history, created_at, updated_at) "
                "VALUES (:code, 'cust-stock', :title, :intent, :status, :history, "
                "'2026-09-01 00:00:00', '2026-09-01 00:00:00')"
            ),
            {"code": code, "title": f"存量单 {code}", "intent": "旧八态工作流存量", "status": status, "history": history},
        )
        ids[code] = conn.execute(
            sa.text("SELECT id FROM demands WHERE code = :code"), {"code": code}
        ).scalar_one()
    return ids


def dump_demands(engine) -> list[tuple]:
    """全行快照（id/status/status_history）——幂等断言的比对基准。"""
    with engine.connect() as conn:
        return conn.execute(
            sa.text("SELECT id, status, status_history FROM demands ORDER BY id")
        ).all()


@pytest.fixture()
def migrated_stock(tmp_path, monkeypatch):
    """0018 库种存量 → upgrade head（0019 归并在 upgrade 内执行）。"""
    db_url = f"sqlite:///{tmp_path}/three_state_migration.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "alembic"))
    command.upgrade(cfg, "0018_plan_limits")

    engine = create_engine(db_url)
    with engine.begin() as conn:
        ids = seed_stock_demands(conn)

    command.upgrade(cfg, "head")
    return db_url, ids


def fetch_row(engine, demand_id: int) -> tuple[str, list]:
    with engine.connect() as conn:
        status, history = conn.execute(
            sa.text("SELECT status, status_history FROM demands WHERE id = :id"),
            {"id": demand_id},
        ).one()
    return status, json.loads(history)


# ------------------------------------------------------- 粗归并


@pytest.mark.parametrize("code", [f"DE-20260901-0{i}" for i in range(1, 6)])
def test_legacy_statuses_merged_to_in_progress(migrated_stock, code):
    """五个废除态单全部 → in_progress；时间线无旧态名且末条是归并留痕。"""
    db_url, ids = migrated_stock
    engine = create_engine(db_url)
    status, history = fetch_row(engine, ids[code])

    assert status == "in_progress"
    # 时间线：submitted 起步、旧态名全部消失、末条为归并留痕（by=migration）
    sequence = [e["status"] for e in history]
    assert sequence[0] == "submitted"
    assert set(sequence) <= {"submitted", "in_progress"}
    assert sequence[-1] == "in_progress"
    assert history[-1]["by"].startswith("migration:")
    assert "归并" in history[-1]["note"]
    # 归并条目文案本身不含旧态名（客户可见时间线纪律）
    for legacy in LEGACY_STATUSES:
        assert legacy not in json.dumps(history, ensure_ascii=False)
    # 连续同态去重：重映射后的连续 in_progress 只保留首条 + 归并留痕
    assert sequence.count("in_progress") <= 2  # 去重后 1 条 + 归并留痕 1 条
    engine.dispose()


def test_delivered_and_rejected_untouched(migrated_stock):
    """终态存量不动：状态不变、不追加归并事件；历史旧态名被清洗。"""
    db_url, ids = migrated_stock
    engine = create_engine(db_url)

    status, history = fetch_row(engine, ids["DE-20260901-06"])
    assert status == "delivered"
    assert [e["status"] for e in history] == ["submitted", "in_progress", "delivered"]
    assert all(e["by"] != "migration:0019_demand_three_state" for e in history)

    status, history = fetch_row(engine, ids["DE-20260901-07"])
    assert status == "rejected"
    assert [e["status"] for e in history] == ["submitted", "in_progress", "rejected"]
    assert all(e["by"] != "migration:0019_demand_three_state" for e in history)
    engine.dispose()


def test_active_stock_and_new_columns(migrated_stock):
    """现行态单不受影响；contact_email/company 新列落地且旧行为 NULL。"""
    db_url, ids = migrated_stock
    engine = create_engine(db_url)

    status, history = fetch_row(engine, ids["DE-20260901-08"])
    assert status == "submitted"
    assert [e["status"] for e in history] == ["submitted"]

    columns = {c["name"]: c for c in inspect(engine).get_columns("demands")}
    assert columns["contact_email"]["nullable"] is True
    assert columns["company"]["nullable"] is True
    with engine.connect() as conn:
        email, company = conn.execute(
            sa.text("SELECT contact_email, company FROM demands WHERE id = :id"),
            {"id": ids["DE-20260901-08"]},
        ).one()
    assert email is None and company is None
    engine.dispose()


def test_chain_head_follows_migration_chain(migrated_stock):
    """链头随迁移链推进（wire-rich-text-docs 起 = 0038_artifact_drafts）。"""
    db_url, _ = migrated_stock
    engine = create_engine(db_url)
    with engine.connect() as conn:
        version = conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
    assert version == "0047_dataset_acl"
    engine.dispose()


# ------------------------------------------------------- 幂等重跑


def test_merge_function_is_idempotent(migrated_stock):
    """归并函数直接重跑：无新事件、无状态再写（全行快照逐字节不变）。"""
    db_url, _ = migrated_stock
    revision = load_revision_module()
    engine = create_engine(db_url)

    before = dump_demands(engine)
    with engine.begin() as conn:
        merged = revision.merge_legacy_demand_statuses(conn)
    after = dump_demands(engine)

    assert merged == 0  # 已无废除态行：归并计数为 0
    assert after == before  # 无新事件、无状态再写

    # 再跑两轮仍稳定（重映射/去重是幂等变换）
    with engine.begin() as conn:
        revision.merge_legacy_demand_statuses(conn)
    assert dump_demands(engine) == before
    engine.dispose()


def test_merge_function_on_fresh_legacy_rows_is_idempotent(tmp_path, monkeypatch):
    """对仍处废除态的库：第一次归并生效，第二次重跑零变更。"""
    db_url = f"sqlite:///{tmp_path}/rerun.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "alembic"))
    command.upgrade(cfg, "head")  # 全链到 0019（空表）

    engine = create_engine(db_url)
    with engine.begin() as conn:
        seed_stock_demands(conn)  # 归并后再手工种回废除态行（模拟补跑场景）

    revision = load_revision_module()
    with engine.begin() as conn:
        merged = revision.merge_legacy_demand_statuses(conn)
    # 归并计数只算状态迁移（5 个废除态行）；终态行的历史清洗不算归并
    assert merged == 5

    snapshot = dump_demands(engine)
    with engine.begin() as conn:
        again = revision.merge_legacy_demand_statuses(conn)
    assert again == 0
    assert dump_demands(engine) == snapshot

    # 全库不再有任何旧态名（状态或历史）
    with engine.connect() as conn:
        for status, history in conn.execute(
            sa.text("SELECT status, status_history FROM demands")
        ).all():
            corpus = status + (history if isinstance(history, str) else json.dumps(history))
            for legacy in LEGACY_STATUSES:
                assert legacy not in corpus
    engine.dispose()
