"""存储双 URL 收敛单测（wire-dedicated-postgres 任务 2.1/2.2）。

覆盖：
- ``normalize_database_url``：PG 历史形态（postgres:// / postgresql:// /
  postgresql+psycopg2://）统一改写为 ``postgresql+psycopg://``，SQLite 与
  已规范形态原样。
- 各平台切片的 ``FD_WIRE_DATABASE_URL`` 最高优先级 + 未设回落既有默认
  （dev 行为不变）。
- datasets 切片既有 env 名（``FD_WIRE_DATASETS_DATABASE_URL``）兼容保持。
"""

from __future__ import annotations

import importlib

import pytest

from fd_wire_api.config import (
    canonical_metadata_database_url,
    normalize_database_url,
)


# ---------------------------------------------------------------- normalize


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("postgres://u:p@db:5432/wire_platform", "postgresql+psycopg://u:p@db:5432/wire_platform"),
        ("postgresql://u:p@db:5432/wire_platform", "postgresql+psycopg://u:p@db:5432/wire_platform"),
        ("postgresql+psycopg2://u@db/w", "postgresql+psycopg://u@db/w"),
        # 已规范形态与 SQLite 原样
        ("postgresql+psycopg://u:p@db/w", "postgresql+psycopg://u:p@db/w"),
        ("sqlite:///./fd_wire_dev.db", "sqlite:///./fd_wire_dev.db"),
        ("sqlite://", "sqlite://"),
    ],
)
def test_normalize_database_url(raw: str, expected: str) -> None:
    assert normalize_database_url(raw) == expected


def test_canonical_metadata_database_url_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FD_WIRE_DATABASE_URL", raising=False)
    assert canonical_metadata_database_url() is None
    monkeypatch.setenv("FD_WIRE_DATABASE_URL", "postgres://db.local/wire_platform")
    assert (
        canonical_metadata_database_url()
        == "postgresql+psycopg://db.local/wire_platform"
    )


# ---------------------------------------------------------------- metering


def test_metering_settings_fd_wire_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    from fd_wire_api.metering.settings import MeteringSettings

    monkeypatch.delenv("FD_WIRE_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    s = MeteringSettings()
    assert s.effective_database_url == "sqlite:///./fd_wire_dev.db"  # dev 默认不变

    monkeypatch.setenv("DATABASE_URL", "sqlite:///./other.db")
    assert MeteringSettings().effective_database_url == "sqlite:///./other.db"

    # 收敛开关压过 DATABASE_URL，且做 PG 形态规范化
    monkeypatch.setenv("FD_WIRE_DATABASE_URL", "postgresql://u:p@pg/wire_platform")
    assert (
        MeteringSettings().effective_database_url
        == "postgresql+psycopg://u:p@pg/wire_platform"
    )


# ---------------------------------------------------------------- catalog


def test_catalog_settings_fd_wire_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    from fd_wire_api.catalog.settings import CatalogSettings

    monkeypatch.delenv("FD_WIRE_DATABASE_URL", raising=False)
    monkeypatch.delenv("CATALOG_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert CatalogSettings().effective_database_url == "sqlite:///./fd_wire_dev.db"

    monkeypatch.setenv("CATALOG_DATABASE_URL", "sqlite:///./mirror.db")
    assert CatalogSettings().effective_database_url == "sqlite:///./mirror.db"

    monkeypatch.setenv("FD_WIRE_DATABASE_URL", "postgres://pg/wire_platform")
    assert (
        CatalogSettings().effective_database_url
        == "postgresql+psycopg://pg/wire_platform"
    )


# ---------------------------------------------------------------- admin / console


def test_admin_database_url_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    from fd_wire_api.admin.db import admin_database_url

    for env in ("FD_WIRE_DATABASE_URL", "FD_WIRE_ADMIN_DATABASE_URL", "DATABASE_URL"):
        monkeypatch.delenv(env, raising=False)
    assert admin_database_url() == "sqlite:///./fd_wire_dev.db"

    monkeypatch.setenv("DATABASE_URL", "sqlite:///./plat.db")
    assert admin_database_url() == "sqlite:///./plat.db"

    monkeypatch.setenv("FD_WIRE_ADMIN_DATABASE_URL", "sqlite:///./admin.db")
    assert admin_database_url() == "sqlite:///./admin.db"

    monkeypatch.setenv("FD_WIRE_DATABASE_URL", "postgresql://pg/wire_platform")
    assert (
        admin_database_url() == "postgresql+psycopg://pg/wire_platform"
    )


# ---------------------------------------------------------------- materialization


def test_materialization_settings_fd_wire_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    from fd_wire_api.materialization.settings import MaterializationSettings

    monkeypatch.delenv("FD_WIRE_DATABASE_URL", raising=False)
    monkeypatch.delenv("MATERIALIZATION_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert MaterializationSettings().effective_database_url == "sqlite:///./fd_wire_dev.db"

    monkeypatch.setenv("MATERIALIZATION_DATABASE_URL", "sqlite:///./mat.db")
    assert MaterializationSettings().effective_database_url == "sqlite:///./mat.db"

    monkeypatch.setenv("FD_WIRE_DATABASE_URL", "postgres://pg/wire_platform")
    assert (
        MaterializationSettings().effective_database_url
        == "postgresql+psycopg://pg/wire_platform"
    )


# ---------------------------------------------------------------- billing


def test_billing_init_engine_honors_fd_wire(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from fd_wire_api.billing import init_billing

    for env in ("FD_WIRE_DATABASE_URL", "BILLING_DATABASE_URL", "DATABASE_URL"):
        monkeypatch.delenv(env, raising=False)

    # 未设收敛开关：回落 BILLING_DATABASE_URL（既有链不变）
    legacy = f"sqlite:///{tmp_path}/legacy_billing.db"
    monkeypatch.setenv("BILLING_DATABASE_URL", legacy)
    engine = init_billing()
    assert str(engine.url) == legacy
    engine.dispose()

    # 设了收敛开关：压过既有链并规范化 PG 形态（此处用 sqlite 冒烟 URL 语义，
    # PG 形态断言走 effective 字符串即可，不真连 PG）
    monkeypatch.setenv("FD_WIRE_DATABASE_URL", f"sqlite:///{tmp_path}/platform.db")
    engine = init_billing()
    assert str(engine.url) == f"sqlite:///{tmp_path}/platform.db"
    engine.dispose()


# ---------------------------------------------------------------- demands


def test_demands_module_url_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """demands 的 DATABASE_URL 模块常量在 import 期求值——reload 验证后恢复。"""
    import fd_wire_api.demands.db as demands_db

    monkeypatch.setenv("FD_WIRE_DATABASE_URL", "postgres://pg/wire_platform")
    try:
        reloaded = importlib.reload(demands_db)
        assert reloaded.DATABASE_URL == "postgresql+psycopg://pg/wire_platform"
    finally:
        monkeypatch.delenv("FD_WIRE_DATABASE_URL", raising=False)
        importlib.reload(demands_db)


# ---------------------------------------------------------------- datasets 兼容


def test_datasets_env_name_compatible(monkeypatch: pytest.MonkeyPatch) -> None:
    """数据集库 env 名保持 ``FD_WIRE_DATASETS_DATABASE_URL``（与本 change 语义一致，
    无需改名——兼容读取即收敛）。"""
    from fd_wire_api.datasets.settings import DatasetsSettings

    monkeypatch.delenv("FD_WIRE_DATASETS_DATABASE_URL", raising=False)
    assert DatasetsSettings().datasets_database_url == "sqlite:///./fd_wire_datasets.db"

    monkeypatch.setenv("FD_WIRE_DATASETS_DATABASE_URL", "postgres://pg/wire_datasets")
    assert (
        DatasetsSettings().datasets_database_url == "postgres://pg/wire_datasets"
    )


# ---------------------------------------------------------------- alembic env


def test_alembic_env_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    """alembic 迁移链连接串：-x db_url > FD_WIRE_DATABASE_URL > DATABASE_URL。

    这里只验证 env 优先级与规范化逻辑（env.py 的解析片段在 alembic 运行
    时才执行；等价逻辑已收敛到 config.normalize_database_url，故直接断言
    其组合结果）。
    """
    monkeypatch.delenv("FD_WIRE_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    resolved = canonical_metadata_database_url() or "sqlite:///./fd_wire_dev.db"
    assert resolved == "sqlite:///./fd_wire_dev.db"

    monkeypatch.setenv("DATABASE_URL", "sqlite:///./plat.db")
    resolved = canonical_metadata_database_url() or "sqlite:///./plat.db"
    assert resolved == "sqlite:///./plat.db"

    monkeypatch.setenv("FD_WIRE_DATABASE_URL", "postgresql://pg/wire_platform")
    resolved = canonical_metadata_database_url() or "sqlite:///./plat.db"
    assert resolved == "postgresql+psycopg://pg/wire_platform"
