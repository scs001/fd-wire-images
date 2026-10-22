"""共享 fixtures：Fake 驱动 + 临时 home + 可注入时钟（全测试无 docker/无网）。"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cell_manager import (
    CellManager,
    CellManagerConfig,
    FakeDriver,
    FakeRegistryClient,
)

# 让 tests 可从仓库任意 cwd 直接跑：确保包根在 sys.path
PKG_ROOT = Path(__file__).resolve().parent.parent
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))


class FakeClock:
    """可控时钟：advance(days=...) 推进，retention 到期测试用。"""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, days: float = 0, seconds: float = 0) -> None:
        self.now += timedelta(days=days, seconds=seconds)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "cell-home"


@pytest.fixture
def config(home: Path) -> CellManagerConfig:
    return CellManagerConfig.from_env(
        env={
            "CELL_MANAGER_HOME": str(home),
            "CELL_IMAGE": "fd-wire/cell-daas:test",
            "CELL_GATEWAY_URL": "https://wire.example.test/mcp",
            "CELL_REGISTRY_URL": "https://registry.example.test",
            "CELL_RETENTION_DAYS": "30",
            "CELL_DRIVER": "fake",
            "CELL_REGISTRY_MODE": "fake",
        }
    )


@pytest.fixture
def fake_driver() -> FakeDriver:
    return FakeDriver()


@pytest.fixture
def fake_registry() -> FakeRegistryClient:
    return FakeRegistryClient()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def manager(config, fake_driver, fake_registry, clock) -> CellManager:
    return CellManager(
        driver=fake_driver,
        registry=fake_registry,
        config=config,
        clock=clock,
    )
