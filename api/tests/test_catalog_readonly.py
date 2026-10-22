"""「仅同步管道可写」的模型层 guard 测试。

- 只读 session（查询侧唯一拿得到的 session）上的任何写尝试——ORM flush、
  ORM-enabled UPDATE/DELETE、Core INSERT、裸 SQL UPDATE——都必须被拒绝。
- 正常读（service.search/get）不受影响。
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, insert, select, text, update

from fd_wire_api.catalog.client import ConceptRecord
from fd_wire_api.catalog.db import MirrorWriteBlocked, readonly_session, sync_session
from fd_wire_api.catalog.models import IndicatorSnapshot
from fd_wire_api.catalog.service import CatalogQueryService
from fd_wire_api.catalog.sync import IndicatorRegistrySync

from test_catalog_common import FakeRegistryClient, catalog_env, make_record  # noqa: F401


def _seed(catalog_env) -> None:
    catalog_env.fake.records = [make_record("cpi_cn", code="CN_CPI")]
    IndicatorRegistrySync(client=catalog_env.fake).run()


def test_orm_add_and_flush_blocked(catalog_env):
    _seed(catalog_env)
    with readonly_session() as session:
        session.add(
            IndicatorSnapshot(
                indicator_id="rogue", code="ROGUE", status="verified", synced_at=None
            )
        )
        with pytest.raises(MirrorWriteBlocked):
            session.flush()


def test_orm_enabled_update_blocked(catalog_env):
    _seed(catalog_env)
    with readonly_session() as session:
        with pytest.raises(MirrorWriteBlocked):
            session.execute(
                update(IndicatorSnapshot).where(IndicatorSnapshot.code == "CN_CPI").values(name_zh="篡改")
            )


def test_orm_enabled_delete_blocked(catalog_env):
    _seed(catalog_env)
    with readonly_session() as session:
        with pytest.raises(MirrorWriteBlocked):
            session.execute(delete(IndicatorSnapshot))


def test_core_insert_blocked(catalog_env):
    from datetime import datetime, timezone

    _seed(catalog_env)
    with readonly_session() as session:
        with pytest.raises(MirrorWriteBlocked):
            session.execute(
                insert(IndicatorSnapshot.__table__).values(
                    indicator_id="rogue2",
                    code="R2",
                    status="verified",
                    synced_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                )
            )


def test_raw_sql_update_blocked(catalog_env):
    _seed(catalog_env)
    with readonly_session() as session:
        with pytest.raises(MirrorWriteBlocked):
            session.execute(text("UPDATE indicator_snapshots SET status = 'verified'"))


def test_blocked_write_leaves_mirror_intact(catalog_env):
    _seed(catalog_env)
    with readonly_session() as session:
        with pytest.raises(MirrorWriteBlocked):
            session.execute(
                update(IndicatorSnapshot).values(status="registered")
            )
    with sync_session() as session:
        row = session.execute(select(IndicatorSnapshot)).scalar_one()
        assert row.status == "verified"  # 未被篡改


def test_service_reads_work_and_have_no_write_api(catalog_env):
    """查询服务可正常读；且其 session 是只读的（guard 生效）。"""
    _seed(catalog_env)
    service = CatalogQueryService()
    result = service.search(q="CPI")
    assert result["total"] == 1
    assert result["items"][0].code == "CN_CPI"
    detail = service.get("cpi_cn")
    assert detail is not None and detail.status == "verified"

    # service 内部 session 即 readonly_session：尝试写被拒
    with readonly_session() as session:
        with pytest.raises(MirrorWriteBlocked):
            session.execute(update(IndicatorSnapshot).values(code="HACK"))


def test_only_sync_pipeline_writes_mirror(catalog_env):
    """同步管道本身（读写 session）可以写——guard 只拦只读侧。"""
    _seed(catalog_env)
    catalog_env.fake.upsert_authority(
        make_record("cpi_cn", code="CN_CPI", status="registered")
    )
    result = IndicatorRegistrySync(client=catalog_env.fake).run()
    assert result.updated == 1
    detail = CatalogQueryService().get("cpi_cn")
    assert detail is not None and detail.status == "registered"  # 权威变更已反映


def test_unknown_status_normalized_conservatively():
    """权威侧非两级状态 → 保守落 registered（不伪装可直读）。"""
    from fd_wire_api.catalog.client import normalize_status

    assert normalize_status("VERIFIED") == "verified"
    assert normalize_status("registered") == "registered"
    assert normalize_status("draft") == "registered"
    assert normalize_status(None) == "registered"
