"""启动期 schema 处置策略的单测（gate-boot-schema-creation 5.1）。

覆盖四类场景 + 非法值回退：
1. 都未设 → 建表开（开发/测试零漂移）
2. 只设总开关 → 总开关说了算
3. 只设逐模块 → 逐模块说了算
4. 两者冲突 → **逐模块优先**
另加：settings 解析出的 False 也算显式关闭；非法值不生效但告警；
校验模式三态 + 非法值回退 warn；``verify_schema`` 的 off/warn/strict 行为；
反射按库缓存（同库只反射一次）。
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, create_engine

from fd_wire_api import db_policy

# ── 1. 开关三级解析 ────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(db_policy.BOOT_AUTO_CREATE_ENV, raising=False)
    monkeypatch.delenv(db_policy.BOOT_SCHEMA_CHECK_ENV, raising=False)
    monkeypatch.delenv("FD_WIRE_CONTACT_AUTO_CREATE_TABLES", raising=False)
    yield


@pytest.fixture
def log_spy(monkeypatch):
    """替身日志器：直接截获 db_policy 的调用。

    不挂 handler、不用 caplog——本仓有测试会改动全局 logging 状态，全量跑时
    handler 收不到记录（单跑却绿），断言会随执行顺序变红变绿。
    """
    records: list[tuple[str, tuple]] = []
    for level in ("debug", "info", "warning", "error"):
        monkeypatch.setattr(
            db_policy._LOG,
            level,
            lambda fmt, *a, _l=level, **k: records.append((_l, (fmt,) + a)),
            raising=False,
        )
    return records


def _text(log_spy) -> str:
    """把截获的日志拼成一行；格式化失败时退回"模板 + 参数"，不让断言因插值崩掉。"""
    parts: list[str] = []
    for _, (fmt, *args) in log_spy:
        try:
            parts.append(fmt % tuple(args) if args else fmt)
        except TypeError:
            parts.append(f"{fmt} :: {args!r}")
    return " | ".join(parts)



def test_default_is_enabled(monkeypatch):
    """都未设 → 开（开发与测试的既有行为不能变）。"""
    assert db_policy.boot_auto_create_enabled("contact", legacy_env="FD_WIRE_CONTACT_AUTO_CREATE_TABLES")


def test_boot_switch_alone_decides(monkeypatch):
    monkeypatch.setenv(db_policy.BOOT_AUTO_CREATE_ENV, "0")
    assert not db_policy.boot_auto_create_enabled("contact", legacy_env="FD_WIRE_CONTACT_AUTO_CREATE_TABLES")
    monkeypatch.setenv(db_policy.BOOT_AUTO_CREATE_ENV, "1")
    assert db_policy.boot_auto_create_enabled("contact", legacy_env="FD_WIRE_CONTACT_AUTO_CREATE_TABLES")


def test_module_switch_alone_decides(monkeypatch):
    monkeypatch.setenv("FD_WIRE_CONTACT_AUTO_CREATE_TABLES", "0")
    assert not db_policy.boot_auto_create_enabled("contact", legacy_env="FD_WIRE_CONTACT_AUTO_CREATE_TABLES")


def test_module_switch_wins_over_boot_switch(monkeypatch):
    """两个方向都要成立：这是"更具体优先"的全部含义。"""
    monkeypatch.setenv(db_policy.BOOT_AUTO_CREATE_ENV, "0")
    monkeypatch.setenv("FD_WIRE_CONTACT_AUTO_CREATE_TABLES", "1")
    assert db_policy.boot_auto_create_enabled("contact", legacy_env="FD_WIRE_CONTACT_AUTO_CREATE_TABLES")

    monkeypatch.setenv(db_policy.BOOT_AUTO_CREATE_ENV, "1")
    monkeypatch.setenv("FD_WIRE_CONTACT_AUTO_CREATE_TABLES", "0")
    assert not db_policy.boot_auto_create_enabled("contact", legacy_env="FD_WIRE_CONTACT_AUTO_CREATE_TABLES")


def test_settings_false_counts_as_explicit_off(monkeypatch):
    """pydantic settings 会读 .env，os.getenv 看不到；它解析出 False 就是显式关。"""
    monkeypatch.setenv(db_policy.BOOT_AUTO_CREATE_ENV, "1")
    assert not db_policy.boot_auto_create_enabled(
        "datasets", legacy_env="FD_WIRE_DATASETS_AUTO_CREATE_TABLES", legacy_value=False
    )


def test_settings_true_does_not_override_boot_off(monkeypatch):
    """settings 的默认 True 不是显式开启，不能压过总开关。"""
    monkeypatch.setenv(db_policy.BOOT_AUTO_CREATE_ENV, "0")
    assert not db_policy.boot_auto_create_enabled(
        "datasets", legacy_env="FD_WIRE_DATASETS_AUTO_CREATE_TABLES", legacy_value=True
    )


def test_unknown_flag_is_ignored_but_warned(monkeypatch, log_spy):
    monkeypatch.setenv("FD_WIRE_CONTACT_AUTO_CREATE_TABLES", "maybe")
    monkeypatch.setenv(db_policy.BOOT_AUTO_CREATE_ENV, "0")
    assert not db_policy.boot_auto_create_enabled("contact", legacy_env="FD_WIRE_CONTACT_AUTO_CREATE_TABLES")
    assert "unrecognized flag" in _text(log_spy)


# ── 2. 校验模式 ────────────────────────────────────────────────────────────


def test_check_mode_default_and_values(monkeypatch):
    assert db_policy.schema_check_mode() == "warn"
    for mode in ("off", "warn", "strict"):
        monkeypatch.setenv(db_policy.BOOT_SCHEMA_CHECK_ENV, mode.upper())
        assert db_policy.schema_check_mode() == mode


def test_check_mode_unknown_falls_back_to_warn(monkeypatch, log_spy):
    monkeypatch.setenv(db_policy.BOOT_SCHEMA_CHECK_ENV, "loud")
    assert db_policy.schema_check_mode() == "warn"
    assert "unrecognized" in _text(log_spy)


# ── 3. verify_schema 三态 ───────────────────────────────────────────────────


@pytest.fixture
def engine():
    db_policy.reset_schema_cache()
    eng = create_engine("sqlite://")
    yield eng
    eng.dispose()
    db_policy.reset_schema_cache()


def _make_table(engine, name):
    metadata = MetaData()
    Table(name, metadata, Column("id", Integer, primary_key=True))
    metadata.create_all(engine)


def test_verify_off_is_silent(engine, monkeypatch, log_spy):
    monkeypatch.setenv(db_policy.BOOT_SCHEMA_CHECK_ENV, "off")
    db_policy.verify_schema("demo", ["absent_table"], engine)
    assert _text(log_spy) == ""


def test_verify_warn_names_missing_tables(engine, monkeypatch, log_spy):
    monkeypatch.setenv(db_policy.BOOT_SCHEMA_CHECK_ENV, "warn")
    _make_table(engine, "present_table")
    db_policy.verify_schema("demo", ["present_table", "absent_a", "absent_b"], engine)
    text = _text(log_spy)
    assert "absent_a" in text and "absent_b" in text
    assert "present_table" not in text


def test_verify_strict_raises_with_names(engine, monkeypatch):
    monkeypatch.setenv(db_policy.BOOT_SCHEMA_CHECK_ENV, "strict")
    with pytest.raises(RuntimeError) as excinfo:
        db_policy.verify_schema("demo", ["absent_table"], engine)
    assert "absent_table" in str(excinfo.value)
    assert "demo" in str(excinfo.value)


def test_verify_strict_passes_when_complete(engine, monkeypatch):
    monkeypatch.setenv(db_policy.BOOT_SCHEMA_CHECK_ENV, "strict")
    _make_table(engine, "present_table")
    db_policy.verify_schema("demo", ["present_table"], engine)  # 不抛


def test_reflection_is_cached_per_database(engine, monkeypatch):
    """同库只反射一次——这是"校验不能变成 25 次往返"的判据。"""
    monkeypatch.setenv(db_policy.BOOT_SCHEMA_CHECK_ENV, "warn")
    calls: list[str] = []

    import sqlalchemy

    real_inspect = sqlalchemy.inspect

    def counting_inspect(target):
        calls.append(str(target))
        return real_inspect(target)

    monkeypatch.setattr(sqlalchemy, "inspect", counting_inspect)
    db_policy.verify_schema("m1", ["t"], engine)
    db_policy.verify_schema("m2", ["t"], engine)
    assert len(calls) == 1


# ── 4. 计时 ────────────────────────────────────────────────────────────────


def test_module_timer_logs_fixed_fields(log_spy):
    with db_policy.module_timer("catalog"):
        pass
    assert "boot.schema module=catalog elapsed=" in _text(log_spy)


def test_boot_timer_logs_total(log_spy):
    with db_policy.boot_timer():
        pass
    assert "boot.schema total elapsed=" in _text(log_spy)
