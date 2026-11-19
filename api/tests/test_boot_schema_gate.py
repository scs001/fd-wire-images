"""启动期建表开关的行为级用例（gate-boot-schema-creation 5.1）。

这些用例不看决策函数，而是**真的调 init_db()**、再查库里有没有表——
判据是「发不发 DDL」，不是「函数返回什么」。
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect

from fd_wire_api import db_policy
from fd_wire_api.contact import db as contact_db

TABLE = "contact_messages"
TABLE_COLUMNS = tuple(
    c.name for c in __import__("fd_wire_api.contact.db", fromlist=["ContactBase"])
    .ContactBase.metadata.tables[TABLE].columns
)


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """contact 切片的独立 sqlite 库（每例一个新文件）。"""
    monkeypatch.setenv("FD_WIRE_CONTACT_DATABASE_URL", f"sqlite:///{tmp_path}/contact.db")
    monkeypatch.delenv(db_policy.BOOT_AUTO_CREATE_ENV, raising=False)
    monkeypatch.delenv("FD_WIRE_CONTACT_AUTO_CREATE_TABLES", raising=False)
    monkeypatch.setenv(db_policy.BOOT_SCHEMA_CHECK_ENV, "off")
    db_policy.reset_schema_cache()
    contact_db.reset_contact_runtime()
    yield contact_db
    contact_db.reset_contact_runtime()
    db_policy.reset_schema_cache()


def _has_table(mod) -> bool:
    engine = mod.get_engine()
    try:
        return TABLE in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_boot_switch_off_issues_no_ddl(isolated_db):
    """总开关关 → 空库上跑完 init_db 仍然没有表（零 DDL）。"""
    import os

    os.environ[db_policy.BOOT_AUTO_CREATE_ENV] = "0"
    try:
        isolated_db.init_db()
        assert not _has_table(isolated_db)
    finally:
        del os.environ[db_policy.BOOT_AUTO_CREATE_ENV]


def test_default_still_creates(isolated_db):
    """都不设 → 照旧建表（开发/测试零漂移）。"""
    isolated_db.init_db()
    assert _has_table(isolated_db)


def test_module_switch_wins_over_boot_switch(isolated_db, monkeypatch):
    """总开关关、逐模块开 → 该模块仍建表。"""
    monkeypatch.setenv(db_policy.BOOT_AUTO_CREATE_ENV, "0")
    monkeypatch.setenv("FD_WIRE_CONTACT_AUTO_CREATE_TABLES", "1")
    isolated_db.init_db()
    assert _has_table(isolated_db)


def test_strict_mode_refuses_to_start(isolated_db, monkeypatch):
    """建表关 + strict + 空库 → 拒绝启动，且错误里点名缺的表。"""
    monkeypatch.setenv(db_policy.BOOT_AUTO_CREATE_ENV, "0")
    monkeypatch.setenv(db_policy.BOOT_SCHEMA_CHECK_ENV, "strict")
    with pytest.raises(RuntimeError) as excinfo:
        isolated_db.init_db()
    assert TABLE in str(excinfo.value)
    assert not _has_table(isolated_db)


# ── entities 切片：与其余十片同规（2026-11-13 补）───────────────────────────
#
# 这一片此前自己读 FD_WIRE_ENTITIES_AUTO_CREATE_TABLES 且默认开：既压过全局
# 开关（生产设为 0），关表时也不做缺表校验。下面四条把"与 contact 同规"钉住，
# 判据仍是「发不发 DDL」，不看决策函数。

from fd_wire_api.entities import db as entities_db  # noqa: E402

ENTITIES_TABLE = "entities"


@pytest.fixture
def isolated_entities_db(tmp_path, monkeypatch):
    monkeypatch.delenv("FD_WIRE_DATABASE_URL", raising=False)  # 收敛口会压过下面这条
    monkeypatch.setenv("FD_WIRE_ENTITIES_DATABASE_URL", f"sqlite:///{tmp_path}/entities.db")
    monkeypatch.delenv(db_policy.BOOT_AUTO_CREATE_ENV, raising=False)
    monkeypatch.delenv("FD_WIRE_ENTITIES_AUTO_CREATE_TABLES", raising=False)
    monkeypatch.setenv(db_policy.BOOT_SCHEMA_CHECK_ENV, "off")
    db_policy.reset_schema_cache()
    entities_db.reset_entities_runtime()
    yield entities_db
    entities_db.reset_entities_runtime()
    db_policy.reset_schema_cache()


def _entities_has_table(mod) -> bool:
    engine = mod.get_engine()
    try:
        return ENTITIES_TABLE in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_entities_boot_switch_off_issues_no_ddl(isolated_entities_db, monkeypatch):
    monkeypatch.setenv(db_policy.BOOT_AUTO_CREATE_ENV, "0")
    isolated_entities_db.init_db()
    assert not _entities_has_table(isolated_entities_db)


def test_entities_default_still_creates(isolated_entities_db):
    isolated_entities_db.init_db()
    assert _entities_has_table(isolated_entities_db)


def test_entities_module_switch_wins_over_boot_switch(isolated_entities_db, monkeypatch):
    monkeypatch.setenv(db_policy.BOOT_AUTO_CREATE_ENV, "0")
    monkeypatch.setenv("FD_WIRE_ENTITIES_AUTO_CREATE_TABLES", "1")
    isolated_entities_db.init_db()
    assert _entities_has_table(isolated_entities_db)


def test_entities_strict_mode_refuses_to_start(isolated_entities_db, monkeypatch):
    monkeypatch.setenv(db_policy.BOOT_AUTO_CREATE_ENV, "0")
    monkeypatch.setenv(db_policy.BOOT_SCHEMA_CHECK_ENV, "strict")
    with pytest.raises(RuntimeError) as excinfo:
        isolated_entities_db.init_db()
    assert ENTITIES_TABLE in str(excinfo.value)
    assert not _entities_has_table(isolated_entities_db)


# ── 列级校验：只比表名时，"加列"迁移在库里是隐形的（2026-11-13 补）──────────
#
# 0054/0055 正是这一类：表都在、列没加。生产实测三列 MISSING 而启动全绿，
# 直到首个读写命中不存在的列才 500。下面两条把那半个方向钉住。

from types import SimpleNamespace  # noqa: E402

COLUMN_CASE_TABLE = "contact_messages"
TABLE_COLUMNS = tuple(
    c.name for c in __import__("fd_wire_api.contact.db", fromlist=["ContactBase"])
    .ContactBase.metadata.tables[TABLE].columns
)
COLUMN_CASE_COLUMN = "made_up_column_zzz"


class _StubTable:
    """verify_schema 只看 ``.columns[*].name``——用 stub 而不是伪造真 Table。"""

    def __init__(self, *names: str) -> None:
        self.columns = [SimpleNamespace(name=n) for n in names]


@pytest.fixture
def contact_with_tables(isolated_db, monkeypatch):
    """表先建好（默认建表开着），再单独验列——不掺建表开关。"""
    isolated_db.init_db()
    assert _has_table(isolated_db)
    yield isolated_db
    monkeypatch.delenv(db_policy.BOOT_SCHEMA_CHECK_ENV, raising=False)


def test_missing_column_warns_in_warn_mode(contact_with_tables, monkeypatch):
    """表在而列缺 → 记一条 missing column，不抛。

    自己挂 handler 而不是用 caplog：``_LOG`` 是 ``uvicorn.error``，跑过全量后它的
    ``propagate`` 已被 uvicorn 配置关掉，caplog 会**收不到**（单跑绿、全量红，
    2026-11-13 实测）。
    """
    import logging

    monkeypatch.setenv(db_policy.BOOT_SCHEMA_CHECK_ENV, "warn")
    records: list[logging.LogRecord] = []

    class _Grab(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Grab()
    logger = logging.getLogger("uvicorn.error")
    # 前面跑过的用例会把 `uvicorn.error` **整个禁用**（`logger.disabled=True`，
    # 由 uvicorn 的 dictConfig 置位）——此时 warning 不进 handler，判据就成了
    # "单跑绿、全量红"的假绿。级别与 disabled 两个开关都要自己按住。
    previous_level = logger.level
    previous_disabled = logger.disabled
    logger.setLevel(logging.WARNING)
    logger.disabled = False
    logger.addHandler(handler)
    try:
        declared = {COLUMN_CASE_TABLE: _StubTable(*TABLE_COLUMNS, COLUMN_CASE_COLUMN)}
        db_policy.verify_schema("contact", declared, contact_with_tables.get_engine())
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logger.disabled = previous_disabled

    assert any(
        "missing column" in r.getMessage() and COLUMN_CASE_COLUMN in r.getMessage()
        for r in records
    ), [r.getMessage() for r in records]


def test_missing_column_refuses_to_start_in_strict_mode(contact_with_tables, monkeypatch):
    """表在而列缺 + strict → 拒绝启动，错误里点名 表.列。"""
    monkeypatch.setenv(db_policy.BOOT_SCHEMA_CHECK_ENV, "strict")
    declared = {COLUMN_CASE_TABLE: _StubTable(*TABLE_COLUMNS, COLUMN_CASE_COLUMN)}
    with pytest.raises(RuntimeError) as excinfo:
        db_policy.verify_schema("contact", declared, contact_with_tables.get_engine())
    assert f"{COLUMN_CASE_TABLE}.{COLUMN_CASE_COLUMN}" in str(excinfo.value)


def test_declared_columns_all_present_stays_quiet(contact_with_tables, monkeypatch):
    """反向对照：列全在时静默通过（否则上面两条就是"永远红"的假判据）。"""
    monkeypatch.setenv(db_policy.BOOT_SCHEMA_CHECK_ENV, "strict")
    declared = {COLUMN_CASE_TABLE: _StubTable(*TABLE_COLUMNS)}
    db_policy.verify_schema("contact", declared, contact_with_tables.get_engine())


# ── 高保真复现：用**真实模型**造出生产现状（表在、三列缺）──────────────────────
#
# 上面的 stub 用例证明"缺列会报"，这一条证明"报的正是 2026-11-13 生产上缺的那三列"。
# 做法不是造夹具，而是拿 EntitiesBase 真建一遍库、再把 0054/0055 加的三列 DROP 掉
# —— 等价于「代码已是新版、库还停在 0053」，也就是那次发布如果顺序做反的状态。


def test_real_models_report_exactly_the_missing_0054_0055_columns(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, text

    from fd_wire_api.entities.db import EntitiesBase

    engine = create_engine(f"sqlite:///{tmp_path}/prod-like.db")
    try:
        EntitiesBase.metadata.create_all(bind=engine)  # 新版模型 = 全列
        with engine.begin() as conn:
            # 退回 0053 的形状：删掉两处迁移新增的列（sqlite ≥3.35 支持 DROP COLUMN）
            conn.execute(text("ALTER TABLE ontology_events DROP COLUMN target_key"))
            conn.execute(text("ALTER TABLE entities DROP COLUMN kernel_attrs"))
            conn.execute(text("ALTER TABLE entity_relationships DROP COLUMN kernel_attrs"))

        monkeypatch.setenv(db_policy.BOOT_SCHEMA_CHECK_ENV, "strict")
        with pytest.raises(RuntimeError) as excinfo:
            db_policy.verify_schema("entities", EntitiesBase.metadata.tables, engine)

        message = str(excinfo.value)
        for expected in (
            "ontology_events.target_key",
            "entities.kernel_attrs",
            "entity_relationships.kernel_attrs",
        ):
            assert expected in message, f"{expected} not named in: {message}"
    finally:
        engine.dispose()
