"""启动期 schema 处置策略（gate-boot-schema-creation）。

一个地方回答三件事，避免 15 个模块各写一份解析：

1. **这一轮启动要不要建表**——``boot_auto_create_enabled(module, ...)``。
   优先级：逐模块开关显式设置 > 启动总开关显式设置 > 缺省开启（开发/测试零漂移）。
   逐模块的 env 名沿用各模块既有命名（``FD_WIRE_CONTACT_AUTO_CREATE_TABLES``、
   ``DEMANDS_AUTO_CREATE_TABLES``……），不为了统一而改名——改名等于让线上既有
   的关闭配置静默失效。
2. **建表关闭时缺表怎么办**——``schema_check_mode()`` + ``verify_schema()``。
   ``off`` 静默 / ``warn`` 逐表告警 / ``strict`` 拒绝启动并点名。
   校验用**每个库一次反射**（``inspect().get_table_names()``），不是逐表
   ``has_table``——后者正是本 change 要消灭的启动开销。
3. **这一模块花了多久**——``module_timer(module)``，一行一模块 + lifespan 末尾一行总计。

设计取舍见 ``openspec/changes/gate-boot-schema-creation/design.md``（D1-D4）。
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any

# 走 uvicorn 已配置的 logger：本模块是库，不自建 handler；而 ``__name__`` 那个
# logger 在 uvicorn 下没有 handler（INFO 会被 lastResort 丢掉，只剩 WARNING 能露头），
# 结果就是"计时在跑但日志里一行都没有"。uvicorn.error 默认 INFO + StreamHandler。
_LOG = logging.getLogger("uvicorn.error")

BOOT_AUTO_CREATE_ENV = "FD_WIRE_BOOT_AUTO_CREATE_TABLES"
BOOT_SCHEMA_CHECK_ENV = "FD_WIRE_BOOT_SCHEMA_CHECK"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})
_CHECK_MODES = ("off", "warn", "strict")
_DEFAULT_CHECK_MODE = "warn"

# 每个库只反射一次：key = inspect(engine).default_schema_name + engine.url（脱敏后）
_reflected_tables: dict[str, set[str]] = {}


def _env_flag(name: str) -> bool | None:
    """读一个布尔开关：未设置返回 None（**必须能区分"未设置"与"设为 0"**）。"""
    raw = os.getenv(name)
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    _LOG.warning(
        "boot.schema unrecognized flag %s=%r (expected one of %s) — ignoring",
        name,
        raw,
        sorted(_TRUTHY | _FALSY),
    )
    return None


def boot_auto_create_enabled(
    module: str,
    *,
    legacy_env: str | None = None,
    legacy_value: bool | None = None,
) -> bool:
    """这一轮启动，``module`` 要不要执行启动建表。

    ``legacy_env``：该模块既有的逐模块开关 env 名（历史命名不统一，逐个给出）。
    ``legacy_value``：调用方用 pydantic settings 解析出来的值（settings 还会读
    ``.env`` 文件，os.getenv 看不到）。仅在它为 **False** 时参与决策——即
    "有人显式关掉了这个模块"；它的默认 True 不代表显式开启，不能压过总开关。

    解析顺序：
      1. ``legacy_env`` 显式 → 以它为准（最具体优先）
      2. ``legacy_value is False`` → 关闭（settings/.env 里关了）
      3. 总开关显式 → 以它为准
      4. 都没有 → True（开发/测试既有行为）
    """
    explicit_legacy = _env_flag(legacy_env) if legacy_env else None
    if explicit_legacy is not None:
        return explicit_legacy
    if legacy_value is False:
        return False
    boot = _env_flag(BOOT_AUTO_CREATE_ENV)
    if boot is not None:
        return boot
    return True


def schema_check_mode() -> str:
    """建表关闭时的缺表校验强度：``off`` / ``warn``（缺省）/ ``strict``。"""
    raw = os.getenv(BOOT_SCHEMA_CHECK_ENV)
    if raw is None:
        return _DEFAULT_CHECK_MODE
    value = raw.strip().lower()
    if value in _CHECK_MODES:
        return value
    _LOG.warning(
        "boot.schema unrecognized %s=%r (expected off|warn|strict) — using %r",
        BOOT_SCHEMA_CHECK_ENV,
        raw,
        _DEFAULT_CHECK_MODE,
    )
    return _DEFAULT_CHECK_MODE


def _cache_key(engine: Any) -> str:
    """反射缓存的键：只取方言与库标识，不含凭据。"""
    url = engine.url
    database = url.database or ""
    host = url.host or ""
    if url.get_backend_name().startswith("sqlite"):
        return f"sqlite::{database}"
    return f"{url.get_backend_name()}::{host}::{database}"


def _reflect_table_names(engine: Any) -> set[str]:
    from sqlalchemy import inspect as sa_inspect

    key = _cache_key(engine)
    cached = _reflected_tables.get(key)
    if cached is not None:
        return cached
    names = set(sa_inspect(engine).get_table_names())
    _reflected_tables[key] = names
    return names


def reset_schema_cache() -> None:
    """清空反射缓存（测试切库用；与各模块 ``reset_*_runtime`` 同辈）。"""
    _reflected_tables.clear()


def _reflect_columns(engine: Any, table: str) -> set[str]:
    """某表实有列（不缓存：列变化只发生在迁移之后，而迁移后进程会重启）。"""
    from sqlalchemy import inspect as sa_inspect

    return {c["name"] for c in sa_inspect(engine).get_columns(table)}


def verify_schema(module: str, table_names: Any, engine: Any) -> None:
    """比对``声明表/列``与库内实有（一次反射，按库缓存表名）。

    ``off`` 直接返回；``warn`` 逐表/逐列 WARNING；``strict`` 抛 ``RuntimeError``
    （消息含缺失的表名与列名，进程非零退出——**不静默降级**，否则缺表/缺列会变成
    首请求 500）。

    **为什么要查列**：只比表名时，一个"加列"迁移（0054 ``ontology_events.target_key``、
    0055 ``kernel_attrs``）在库里**完全隐形**——表都在，启动全绿，直到首个读写命中
    不存在的列才 500。2026-11-13 实测生产就是这个状态（三列 MISSING、head 落后两版）。

    参数兼容两种写法：``Base.metadata.tables``（名字→Table，能拿到列）或单纯的名字
    集合（只有表名，不做列校验）。
    """
    from collections.abc import Mapping as _Mapping

    mode = schema_check_mode()
    if mode == "off":
        return

    declared: dict[str, set[str] | None] = {}
    if isinstance(table_names, _Mapping):
        for name, table in table_names.items():
            columns = getattr(table, "columns", None)
            declared[str(name)] = {c.name for c in columns} if columns is not None else None
    else:
        for name in table_names or ():
            if name:
                declared[str(name)] = None
    declared = {name: cols for name, cols in declared.items() if name}
    if not declared:
        return

    try:
        existing = _reflect_table_names(engine)
    except Exception as exc:  # 反射失败不能装成"没缺表"
        _LOG.warning("boot.schema module=%s verify skipped (reflection failed: %s)", module, exc)
        return

    missing_tables = sorted(set(declared) - existing)

    # 列只在表真的存在时才比：表缺了，"缺列"是同一件事的另一种说法，不重复报。
    missing_columns: dict[str, list[str]] = {}
    for name, cols in declared.items():
        if cols is None or name in missing_tables:
            continue
        try:
            actual = _reflect_columns(engine, name)
        except Exception as exc:
            _LOG.warning(
                "boot.schema module=%s column check skipped for %s (reflection failed: %s)",
                module, name, exc,
            )
            continue
        gap = sorted(cols - actual)
        if gap:
            missing_columns[name] = gap

    if not missing_tables and not missing_columns:
        return

    if mode == "strict":
        parts = []
        if missing_tables:
            parts.append(f"missing tables {missing_tables}")
        if missing_columns:
            rendered = ", ".join(f"{t}.{c}" for t, cols in missing_columns.items() for c in cols)
            parts.append(f"missing columns [{rendered}]")
        raise RuntimeError(
            f"boot schema check failed for module={module}: {'; '.join(parts)}. "
            "Run alembic upgrade head (or set the module's *_AUTO_CREATE_TABLES=1) before starting."
        )
    for name in missing_tables:
        _LOG.warning("boot.schema missing table=%s module=%s", name, module)
    for name, cols in missing_columns.items():
        for col in cols:
            _LOG.warning("boot.schema missing column=%s.%s module=%s", name, col, module)


@contextmanager
def module_timer(module: str) -> Iterator[None]:
    """给一个模块的启动期 schema 处置计时，结束打一行（字段固定，便于 grep）。"""
    started = time.perf_counter()
    try:
        yield
    finally:
        _LOG.info("boot.schema module=%s elapsed=%.2fs", module, time.perf_counter() - started)


def boot_total(started: float) -> None:
    """lifespan 末尾打一行总耗时（配合 ``time.perf_counter()` 起点）。"""
    _LOG.info("boot.schema total elapsed=%.2fs", time.perf_counter() - started)


@contextmanager
def boot_timer() -> Iterator[None]:
    """lifespan 末尾打一行总耗时。"""
    started = time.perf_counter()
    try:
        yield
    finally:
        _LOG.info("boot.schema total elapsed=%.2fs", time.perf_counter() - started)
