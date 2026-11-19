"""catalog 镜像测试的共享环境（本文件自身不含测试）。

提供：
- ``FakeRegistryClient``：权威源假实现（``list_registry_entries`` 契约的
  内存等价物：每次 ``fetch_concepts()`` 全量返回当前 records），同步器测试
  以它驱动全量重刷/对账/防御路径；
- ``catalog_env`` fixture：隔离的 SQLite 镜像库 + 挂好 catalog 路由的
  FastAPI 应用（同步器注入假客户端，调度关闭；拉取条数防御降为 0，
  防御路径本身在 test_catalog_sync 里显式以 min_total=1000 触发）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fd_wire_api.catalog import catalog_settings, init_db, register_catalog, reset_engines
from fd_wire_api.catalog.client import ConceptBatch, ConceptRecord, utcnow
from fd_wire_api.catalog.routes import get_registry_client

INTERNAL_TOKEN = "test-internal-token"


def make_record(indicator_id: str, **overrides) -> ConceptRecord:
    """带默认展示字段的权威记录（indicator_id 形如 ``source_db:native_code``）。"""
    base = dict(
        source_db=indicator_id.split(":", 1)[0] if ":" in indicator_id else "gta",
        native_code=indicator_id.split(":", 1)[1] if ":" in indicator_id else indicator_id,
        code=indicator_id.upper(),
        name_zh=f"名称-{indicator_id}",
        name_en=f"Name {indicator_id}",
        description_zh=f"描述-{indicator_id}",
        description_en=f"Description {indicator_id}",
        domain="macro",
        frequency="monthly",
        unit="指数点",
        time_span_start="2000-01",
        time_span_end="2026-09",
        entity_coverage_count=31,
        sample_value="102.3",
        status="verified",
        updated_at=utcnow(),
    )
    base.update(overrides)
    return ConceptRecord(indicator_id=indicator_id, **base)


class FakeRegistryClient:
    """权威源假实现：fetch_concepts() 每次全量返回当前 records（与
    list_registry_entries 的无游标全量枚举同语义）；记录调用次数。"""

    def __init__(self, records: list[ConceptRecord] | None = None):
        self.records: list[ConceptRecord] = list(records or [])
        self.calls: int = 0

    def upsert_authority(self, record: ConceptRecord) -> None:
        """模拟权威库新增/变更（以 indicator_id 为准）。"""
        self.records = [r for r in self.records if r.indicator_id != record.indicator_id]
        self.records.append(record)

    def remove_authority(self, indicator_id: str) -> None:
        self.records = [r for r in self.records if r.indicator_id != indicator_id]

    def fetch_concepts(self) -> ConceptBatch:
        self.calls += 1
        return ConceptBatch(items=list(self.records), full=True, next_watermark=utcnow())


@pytest.fixture
def catalog_env(tmp_path, monkeypatch):
    """隔离镜像库 + 应用 + 假权威源。"""
    db_url = f"sqlite:///{tmp_path/'catalog_mirror.db'}"
    monkeypatch.setattr(catalog_settings, "catalog_database_url", db_url)
    monkeypatch.setattr(catalog_settings, "database_url", None)
    monkeypatch.setattr(catalog_settings, "catalog_internal_token", INTERNAL_TOKEN)
    monkeypatch.setattr(catalog_settings, "catalog_sync_enabled", False)
    monkeypatch.setattr(catalog_settings, "catalog_sync_on_startup", False)
    # 测试数据只有个位数条目：条数防御降为 0（防御路径显式测，见 sync 测试）
    monkeypatch.setattr(catalog_settings, "catalog_sync_min_total", 0)
    reset_engines()
    init_db()

    app = FastAPI()
    register_catalog(app)
    fake = FakeRegistryClient()
    app.dependency_overrides[get_registry_client] = lambda: fake
    client = TestClient(app)

    env = SimpleNamespace(
        app=app,
        client=client,
        fake=fake,
        db_url=db_url,
        internal_token=INTERNAL_TOKEN,
    )
    yield env
    reset_engines()
