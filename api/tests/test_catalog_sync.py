"""同步器测试：单事务全量重刷、幂等、对账退役（含旧格式 ID）、<1000 防御、失败留痕。"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from fd_wire_api.catalog.db import sync_session
from fd_wire_api.catalog.models import CatalogSyncState, IndicatorSnapshot
from fd_wire_api.catalog.sync import IndicatorRegistrySync

from test_catalog_common import FakeRegistryClient, catalog_env, make_record  # noqa: F401


def _rows():
    with sync_session() as session:
        return list(session.execute(select(IndicatorSnapshot)).scalars())


def _state() -> CatalogSyncState:
    with sync_session() as session:
        return session.get(CatalogSyncState, 1)


def test_full_sync_lands_all_fields(catalog_env):
    catalog_env.fake.records = [
        make_record("gta:cpi_cn", code="CN_CPI_YOY", status="verified"),
        make_record("gta:pmi_cn", code="CN_PMI", status="registered"),
    ]
    result = IndicatorRegistrySync(client=catalog_env.fake).run()

    assert result.mode == "full"
    assert (result.created, result.updated, result.removed) == (2, 0, 0)
    assert result.total_in_mirror == 2
    rows = {r.indicator_id: r for r in _rows()}
    assert set(rows) == {"gta:cpi_cn", "gta:pmi_cn"}
    row = rows["gta:cpi_cn"]
    # 全部展示字段按权威记录落库（含新直映的 source_db / unit）
    assert row.source_db == "gta"
    assert row.code == "CN_CPI_YOY"  # code ← semantic_code（展示属性，非主键）
    assert row.name_zh == "名称-gta:cpi_cn"
    assert row.name_en == "Name gta:cpi_cn"
    assert row.description_zh and row.description_en
    assert row.domain == "macro"
    assert row.frequency == "monthly"
    assert row.unit == "指数点"
    assert row.time_span_start == "2000-01" and row.time_span_end == "2026-09"
    assert row.entity_coverage_count == 31
    assert row.sample_value == "102.3"
    assert row.status == "verified"
    assert row.synced_at is not None
    assert rows["gta:pmi_cn"].status == "registered"
    # 同步状态落库
    assert _state().last_watermark is not None
    assert _state().last_status == "ok"


def test_duplicate_indicator_id_upserts_single_row(catalog_env):
    catalog_env.fake.records = [
        make_record("gta:dup", code="OLD"),
        make_record("gta:dup", code="NEW", status="registered"),
    ]
    result = IndicatorRegistrySync(client=catalog_env.fake).run()

    assert result.created == 1  # 权威主键唯一：同 id 只有一行
    rows = _rows()
    assert len(rows) == 1
    assert rows[0].code == "NEW"  # 批内后到者胜出


def test_resync_reflects_authority_change(catalog_env):
    """权威变更次日反映 = 次日重跑 sync 后镜像可见（恒全量重刷，无增量路径）。"""
    syncer = IndicatorRegistrySync(client=catalog_env.fake)
    catalog_env.fake.records = [
        make_record("gta:cpi_cn", status="verified"),
        make_record("gta:pmi_cn", status="registered"),
    ]
    syncer.run()

    # —— 权威库发生新增 / 状态变更 / 字段更新 ——
    catalog_env.fake.upsert_authority(
        make_record("gta:coal", code="CN_COAL", domain="energy", status="registered")
    )
    catalog_env.fake.upsert_authority(
        make_record("gta:cpi_cn", status="verified", entity_coverage_count=42)
    )
    catalog_env.fake.upsert_authority(
        make_record("gta:pmi_cn", status="verified")  # registered → verified
    )

    result = syncer.run()
    assert result.mode == "full"
    assert (result.created, result.updated, result.removed) == (1, 2, 0)

    rows = {r.indicator_id: r for r in _rows()}
    assert rows["gta:coal"].domain == "energy" and rows["gta:coal"].status == "registered"
    assert rows["gta:cpi_cn"].entity_coverage_count == 42
    assert rows["gta:pmi_cn"].status == "verified"
    assert _state().last_status == "ok"


def test_resync_idempotent(catalog_env):
    """幂等：权威无变更时重刷不新增不删除，镜像总量不变。"""
    syncer = IndicatorRegistrySync(client=catalog_env.fake)
    catalog_env.fake.records = [make_record("gta:a"), make_record("gta:b")]
    first = syncer.run()
    assert (first.created, first.updated, first.removed) == (2, 0, 0)

    second = syncer.run()
    assert (second.created, second.updated, second.removed) == (0, 2, 0)
    assert second.total_in_mirror == 2
    assert {r.indicator_id for r in _rows()} == {"gta:a", "gta:b"}
    assert second.watermark is not None


def test_resync_retires_entries_absent_from_authority(catalog_env):
    """对账：本次未出现的旧条目一律退役——旧拼接格式 ID 存量一次性清空。"""
    syncer = IndicatorRegistrySync(client=catalog_env.fake)
    # 旧通道遗留：拼接格式 ID（code@entity_type@…）+ 语义码重复的旧条目
    catalog_env.fake.records = [
        make_record("GDP_TOTAL@country@nominal_current@USD@annual", code="GDP_TOTAL"),
        make_record("legacy_only", code="LEGACY"),
    ]
    syncer.run()
    assert {r.indicator_id for r in _rows()} == {
        "GDP_TOTAL@country@nominal_current@USD@annual",
        "legacy_only",
    }

    # 换源后的权威全量：只有 source_db:native_code 新标识 → 旧格式全部退役
    catalog_env.fake.records = [
        make_record("gta:gdp_total", code="GDP_TOTAL"),
        make_record("world_bank:WB.GDP", code="GDP"),
    ]
    result = syncer.run()
    assert result.removed == 2
    assert result.created == 2
    assert result.total_in_mirror == 2
    assert {r.indicator_id for r in _rows()} == {"gta:gdp_total", "world_bank:WB.GDP"}


def test_cross_source_same_semantic_code_no_collision(catalog_env):
    """跨源等价条目（语义码相同、source_db 不同）标识不同、互不覆盖。"""
    catalog_env.fake.records = [
        make_record("gta:gdp", code="GDP", name_zh="GTA 的 GDP"),
        make_record("world_bank:gdp", code="GDP", name_zh="WB 的 GDP"),
    ]
    result = IndicatorRegistrySync(client=catalog_env.fake).run()

    assert result.created == 2  # 语义码跨源可重复：不当主键，两行并存
    rows = {r.indicator_id: r for r in _rows()}
    assert set(rows) == {"gta:gdp", "world_bank:gdp"}
    assert rows["gta:gdp"].code == rows["world_bank:gdp"].code == "GDP"
    assert rows["gta:gdp"].name_zh == "GTA 的 GDP"
    assert rows["world_bank:gdp"].name_zh == "WB 的 GDP"


def test_fetch_below_min_total_aborts_and_keeps_mirror(catalog_env):
    """异常防御：拉取总条数 < min_total 视为上游异常——中止且不提交，旧镜像原样。"""
    seeder = IndicatorRegistrySync(client=catalog_env.fake, min_total=0)
    catalog_env.fake.records = [
        make_record("gta:cpi_cn", status="verified"),
        make_record("gta:pmi_cn", status="registered"),
    ]
    seeder.run()
    rows_before = {r.indicator_id: r.code for r in _rows()}
    state_before = _state()
    watermark_before = state_before.last_watermark
    success_before = state_before.last_success_at

    # 上游「异常」：权威全量只剩 1 条（< 1,000 常识下限）
    catalog_env.fake.records = [make_record("gta:cpi_cn")]
    defender = IndicatorRegistrySync(client=catalog_env.fake, min_total=1000)
    with pytest.raises(RuntimeError, match="too small"):
        defender.run()

    # 旧镜像原样保留（空目录灾难被挡下）
    rows_after = {r.indicator_id: r.code for r in _rows()}
    assert rows_after == rows_before
    assert len(rows_after) == 2
    state = _state()
    assert state.last_status == "error"
    assert "too small" in (state.last_error or "")
    assert state.last_watermark == watermark_before  # 水位不推进
    assert state.last_success_at == success_before

    # 上游恢复（≥ min_total）后下一次同步照常重刷
    catalog_env.fake.records = [
        make_record("gta:cpi_cn"),
        make_record("gta:pmi_cn"),
    ] + [make_record(f"src:filler_{i:04d}") for i in range(1000)]
    recovered = IndicatorRegistrySync(client=catalog_env.fake, min_total=1000).run()
    assert recovered.mode == "full"
    assert recovered.total_in_mirror == 1002
    assert _state().last_status == "ok"


def test_sync_failure_recorded_and_reraised(catalog_env):
    class ExplodingClient(FakeRegistryClient):
        def fetch_concepts(self):
            raise RuntimeError("authority down")

    syncer = IndicatorRegistrySync(client=ExplodingClient())
    with pytest.raises(RuntimeError, match="authority down"):
        syncer.run()

    state = _state()
    assert state.last_status == "error"
    assert "authority down" in (state.last_error or "")
    assert state.last_watermark is None  # 失败不推进水位


def test_min_total_defaults_from_settings(catalog_env):
    """min_total 缺省读 settings（生产 1,000；fixture 把它降为 0 以便小数据集）。"""
    catalog_env.fake.records = [make_record("gta:cpi_cn")]
    # settings.catalog_sync_min_total == 0（fixture）→ 单条也可入库
    result = IndicatorRegistrySync(client=catalog_env.fake).run()
    assert result.created == 1
    assert result.mode == "full"
