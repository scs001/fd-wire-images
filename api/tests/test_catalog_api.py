"""目录 API 测试：公开搜索/过滤/分页/详情、导流字段、stats 计数、internal 触发与保护。"""

from __future__ import annotations

from test_catalog_common import catalog_env, make_record  # noqa: F401

TOKEN_HEADER = {"X-Internal-Token": "test-internal-token"}


def _seed_three(catalog_env) -> None:
    catalog_env.fake.records = [
        make_record("cpi_cn", code="CN_CPI_YOY", domain="macro", status="verified"),
        make_record("pmi_cn", code="CN_PMI", domain="macro", status="registered"),
        make_record(
            "coal_output",
            code="CN_COAL",
            domain="energy",
            status="registered",
            source_db="world_bank",
            native_code="WB.COAL",
        ),
    ]
    resp = catalog_env.client.post("/internal/catalog/sync", headers=TOKEN_HEADER)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---- internal 触发端点：token 保护 ----


def test_sync_requires_token(catalog_env):
    resp = catalog_env.client.post("/internal/catalog/sync")
    assert resp.status_code == 403
    resp = catalog_env.client.post(
        "/internal/catalog/sync", headers={"X-Internal-Token": "wrong"}
    )
    assert resp.status_code == 403


def test_sync_rejected_when_token_unconfigured(catalog_env, monkeypatch):
    from fd_wire_api.catalog import catalog_settings

    monkeypatch.setattr(catalog_settings, "catalog_internal_token", "")
    resp = catalog_env.client.post("/internal/catalog/sync", headers=TOKEN_HEADER)
    assert resp.status_code == 403


def test_internal_sync_returns_summary(catalog_env):
    summary = _seed_three(catalog_env)
    # 同步恒为单事务全量重刷（mode 恒 full）
    assert summary["mode"] == "full"
    assert summary["created"] == 3
    assert summary["total_in_mirror"] == 3
    assert summary["watermark"] is not None

    # 再触发一次：权威无变更 → 全量重刷幂等（不新增不删除）
    resp = catalog_env.client.post("/internal/catalog/sync", headers=TOKEN_HEADER)
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "full"
    assert body["fetched"] == 3
    assert (body["created"], body["updated"], body["removed"]) == (0, 3, 0)
    assert body["total_in_mirror"] == 3

    # force_full 参数兼容保留（如今本就恒全量）
    resp = catalog_env.client.post(
        "/internal/catalog/sync", headers=TOKEN_HEADER, json={"force_full": True}
    )
    assert resp.status_code == 200
    assert resp.json()["mode"] == "full"


# ---- stats：公开匿名计数端点（数字活取的唯一出口） ----


def test_stats_public_anonymous_matches_mirror(catalog_env):
    _seed_three(catalog_env)
    # 无任何鉴权头 → 200（公开匿名可访问）
    resp = catalog_env.client.get("/api/v1/catalog/stats")
    assert resp.status_code == 200
    body = resp.json()
    # registered＝登记总量（全量，含 verified）；verified 为其中已核验数
    assert body["registered"] == 3
    assert body["verified"] == 1
    by_source = {row["source_db"]: row for row in body["by_source"]}
    assert by_source == {
        "gta": {"source_db": "gta", "registered": 2, "verified": 1},
        "world_bank": {"source_db": "world_bank", "registered": 1, "verified": 0},
    }
    # 分组计数与镜像列表 total 一致
    listing = catalog_env.client.get("/api/v1/catalog/indicators").json()
    assert body["registered"] == listing["total"]


def test_stats_reflects_resync(catalog_env):
    """镜像完成一次同步后，stats 随之更新（与镜像查询结果一致）。"""
    _seed_three(catalog_env)
    catalog_env.fake.remove_authority("coal_output")
    catalog_env.fake.upsert_authority(
        make_record("pmi_cn", code="CN_PMI", status="verified")
    )
    resp = catalog_env.client.post("/internal/catalog/sync", headers=TOKEN_HEADER)
    assert resp.status_code == 200

    body = catalog_env.client.get("/api/v1/catalog/stats").json()
    assert body["registered"] == 2
    assert body["verified"] == 2  # pmi registered → verified
    by_source = {row["source_db"]: row for row in body["by_source"]}
    assert set(by_source) == {"gta"}  # world_bank 条目已退役，分组随之消失


def test_stats_empty_mirror_is_zeroed(catalog_env):
    resp = catalog_env.client.get("/api/v1/catalog/stats")
    assert resp.status_code == 200
    assert resp.json() == {"registered": 0, "verified": 0, "by_source": []}


# ---- 公开列表：搜索 / 过滤 / 分页（不登录） ----


def test_list_all_unauthenticated(catalog_env):
    _seed_three(catalog_env)
    resp = catalog_env.client.get("/api/v1/catalog/indicators")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert body["page"] == 1 and body["page_size"] == 20
    codes = {item["code"] for item in body["items"]}
    assert codes == {"CN_CPI_YOY", "CN_PMI", "CN_COAL"}
    # 列表项不含观测数值类字段；含新增的 source_db / unit 属性
    assert set(body["items"][0]) == {
        "indicator_id",
        "source_db",
        "code",
        "name_zh",
        "name_en",
        "domain",
        "frequency",
        "unit",
        "status",
    }
    # 来源库独立成列（不解析 ID）
    by_code = {item["code"]: item for item in body["items"]}
    assert by_code["CN_COAL"]["source_db"] == "world_bank"
    assert by_code["CN_CPI_YOY"]["source_db"] == "gta"


def test_keyword_search_hits_names_descriptions_code(catalog_env):
    _seed_three(catalog_env)
    base = "/api/v1/catalog/indicators"

    assert catalog_env.client.get(base, params={"q": "居民消费"}).json()["total"] == 0  # 名称不含
    # 中文名（名称-cpi_cn）
    assert catalog_env.client.get(base, params={"q": "名称-cpi"}).json()["total"] == 1
    # 英文名 / 描述
    assert catalog_env.client.get(base, params={"q": "Name coal"}).json()["total"] == 1
    assert catalog_env.client.get(base, params={"q": "描述-pmi"}).json()["total"] == 1
    assert catalog_env.client.get(base, params={"q": "Description energy"}).json()["total"] == 0
    # 代码（不区分大小写）
    assert catalog_env.client.get(base, params={"q": "cn_cpi"}).json()["total"] == 1
    # 落空
    assert catalog_env.client.get(base, params={"q": "不存在"}).json()["total"] == 0
    assert catalog_env.client.get(base, params={"q": "ZZZ_NO_SUCH"}).json()["total"] == 0


def test_domain_and_status_filters(catalog_env):
    _seed_three(catalog_env)
    base = "/api/v1/catalog/indicators"

    body = catalog_env.client.get(base, params={"domain": "energy"}).json()
    assert body["total"] == 1 and body["items"][0]["code"] == "CN_COAL"

    body = catalog_env.client.get(base, params={"status": "verified"}).json()
    assert body["total"] == 1 and body["items"][0]["code"] == "CN_CPI_YOY"

    body = catalog_env.client.get(base, params={"status": "registered"}).json()
    assert body["total"] == 2

    body = catalog_env.client.get(
        base, params={"domain": "macro", "status": "registered"}
    ).json()
    assert body["total"] == 1 and body["items"][0]["code"] == "CN_PMI"

    # 组合关键词
    body = catalog_env.client.get(
        base, params={"status": "registered", "q": "名称-coal"}
    ).json()
    assert body["total"] == 1

    # 非法 status 被请求校验拒绝
    assert catalog_env.client.get(base, params={"status": "draft"}).status_code == 422


def test_pagination(catalog_env):
    _seed_three(catalog_env)
    base = "/api/v1/catalog/indicators"

    page1 = catalog_env.client.get(base, params={"page": 1, "page_size": 2}).json()
    page2 = catalog_env.client.get(base, params={"page": 2, "page_size": 2}).json()
    page3 = catalog_env.client.get(base, params={"page": 3, "page_size": 2}).json()
    assert page1["total"] == 3 and len(page1["items"]) == 2
    assert len(page2["items"]) == 1
    assert page3["items"] == []
    assert page1["items"][0]["code"] <= page1["items"][1]["code"]  # 排序稳定
    # 分页参数越界被校验拒绝
    assert catalog_env.client.get(base, params={"page": 0}).status_code == 422
    assert catalog_env.client.get(base, params={"page_size": 101}).status_code == 422


# ---- 详情：全部展示字段 + registered 导流 + 无观测数值 ----


def test_detail_all_display_fields(catalog_env):
    _seed_three(catalog_env)
    resp = catalog_env.client.get("/api/v1/catalog/indicators/cpi_cn")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "indicator_id",
        "source_db",
        "code",
        "name_zh",
        "name_en",
        "description_zh",
        "description_en",
        "domain",
        "frequency",
        "unit",
        "time_span_start",
        "time_span_end",
        "entity_coverage_count",
        "sample_value",
        "status",
        "synced_at",
        "on_demand",
        "demand_prefill",
    }
    assert body["source_db"] == "gta"
    assert body["code"] == "CN_CPI_YOY"
    assert body["unit"] == "指数点"
    assert body["time_span_start"] == "2000-01" and body["time_span_end"] == "2026-09"
    assert body["entity_coverage_count"] == 31
    assert body["sample_value"] == "102.3"
    assert body["status"] == "verified"
    # verified：无导流
    assert body["on_demand"] is False
    assert body["demand_prefill"] is None


def test_detail_by_indicator_id_and_numeric_surrogate(catalog_env):
    """详情可用 indicator_id（source_db:native_code 形态）或镜像代理数字 id 寻址。"""
    _seed_three(catalog_env)
    # make_record 以传入 id 为 indicator_id（此处 "cpi_cn"）
    by_ref = catalog_env.client.get("/api/v1/catalog/indicators/cpi_cn").json()
    assert by_ref["indicator_id"] == "cpi_cn"
    # 换源后的权威主键形态 source_db:native_code 同样可寻址（URL 冒号合法）
    catalog_env.fake.records = catalog_env.fake.records + [
        make_record("world_bank:WB.COAL", code="CN_COAL2", domain="energy")
    ]
    catalog_env.client.post("/internal/catalog/sync", headers=TOKEN_HEADER)
    colon = catalog_env.client.get("/api/v1/catalog/indicators/world_bank:WB.COAL")
    assert colon.status_code == 200
    assert colon.json()["indicator_id"] == "world_bank:WB.COAL"
    assert colon.json()["source_db"] == "world_bank"
    # 首次全量同步按权威顺序落库：cpi_cn 为第 1 行（SQLite 自增从 1 起）
    resp = catalog_env.client.get("/api/v1/catalog/indicators/1")
    assert resp.status_code == 200
    assert resp.json()["indicator_id"] == by_ref["indicator_id"]
    # 越界数字 id → 404
    assert catalog_env.client.get("/api/v1/catalog/indicators/999").status_code == 404


def test_detail_registered_on_demand_prefill(catalog_env):
    _seed_three(catalog_env)
    body = catalog_env.client.get("/api/v1/catalog/indicators/pmi_cn").json()
    assert body["status"] == "registered"
    # 导流字段：按需开通 + 需求单预填标识（indicator code＝语义码）
    assert body["on_demand"] is True
    assert body["demand_prefill"] == {"indicator_code": "CN_PMI"}

    coal = catalog_env.client.get("/api/v1/catalog/indicators/coal_output").json()
    assert coal["on_demand"] is True
    assert coal["demand_prefill"]["indicator_code"] == "CN_COAL"


def test_detail_unknown_404(catalog_env):
    _seed_three(catalog_env)
    resp = catalog_env.client.get("/api/v1/catalog/indicators/nope")
    assert resp.status_code == 404


def test_no_observation_values_anywhere(catalog_env):
    """目录 API（含 stats）不返回任何观测数值（镜像表也没有观测列）。"""
    _seed_three(catalog_env)
    for path in (
        "/api/v1/catalog/indicators",
        "/api/v1/catalog/indicators/cpi_cn",
        "/api/v1/catalog/indicators/pmi_cn",
        "/api/v1/catalog/stats",
    ):
        payload = catalog_env.client.get(path).text
        for forbidden in ("observations", "values", "series", "data_points"):
            assert forbidden not in payload
