"""源包 API 测试（wire-source-packs，tasks 1.1-1.3 / 2.1-2.3）。

覆盖：
- 公开包列表（不登录）：8 包齐全、各包指标数=filter 实时切片数、字段面；
- 公开包详情：分页（默认 1/20、越界 422）、清单字段 {code,name_zh,domain}、
  未知包 404；跨源重叠切片（domain 包计入 fd_open_data 行）；
- 登录订阅面：未登录 401；订阅幂等（重复 200 不多写行）；退订幂等；
  跨客户隔离；未知包 subscribe/unsubscribe 一律 404；
- filter 的 codes 分支（服务层直测）；
- 迁移链 0022：建表/索引/唯一约束、幂等重跑、与 ORM 漂移检查、降级。
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

# 在导入 app 相关模块前钉住 packs 订阅库：测试进程内任何 lifespan 的
# init_packs_db 都落在内存库，不在测试机上落下 fd_wire_packs.db 文件。
os.environ.setdefault("FD_WIRE_PACKS_DATABASE_URL", "sqlite://")

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fd_wire_api.catalog import catalog_settings, init_db as init_catalog_db
from fd_wire_api.catalog import register_catalog, reset_engines
from fd_wire_api.catalog.routes import get_registry_client
from fd_wire_api.packs import init_db as init_packs_db
from fd_wire_api.packs import reset_packs_runtime, router as packs_router
from fd_wire_api.packs.definitions import PACKS, PackDefinition
from fd_wire_api.packs.service import PackQueryService

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token
from test_catalog_common import FakeRegistryClient, make_record

TOKEN_HEADER = {"X-Internal-Token": "test-internal-token"}
SUB_A = "sub-pack-001"
SUB_B = "sub-pack-002"

API_DIR = Path(__file__).resolve().parent.parent

#: tests seed 下各包预期指标数（见 _seed_records；重叠切片如实双计）
EXPECTED_COUNTS = {
    "macro-prices": 2,
    "population-employment": 2,  # 就业 1 + fd_open_data 的就业域 1（跨源重叠）
    "money-finance": 4,
    "foreign-trade": 2,
    "industry-activity": 6,      # 五域各 1 + fd_open_data 的 PMI 域 1（跨源重叠）
    "regional-economy": 5,
    "yearbook-panorama": 7,
    "open-data-panorama": 6,
}


def _seed_records() -> list:
    """seed：覆盖全部 8 包的 filter 维度（domain / source_db / 跨源重叠）。"""
    records = [
        # domain 切片（source_db 走 make_record 默认 gta）
        make_record("cpi-yoy", code="CN_CPI_YOY", domain="价格指数"),
        make_record("ppi-yoy", code="CN_PPI_YOY", domain="价格指数"),
        make_record("emp-nfp", code="US_NFP", domain="就业"),
        make_record("m2", code="CN_M2", domain="货币金融"),
        make_record("rrr", code="CN_RRR", domain="货币金融"),
        make_record("lpr", code="CN_LPR", domain="利率"),
        make_record("btc", code="BTC_USD", domain="加密货币"),
        make_record("exp", code="CN_EXPORT", domain="对外贸易"),
        make_record("imp", code="CN_IMPORT", domain="对外贸易"),
        make_record("pmi", code="CN_PMI", domain="PMI"),
        make_record("ip-va", code="CN_IP", domain="工业"),
        make_record("elec", code="CN_ELEC", domain="能源"),
        make_record("hpi", code="CN_HPI", domain="房地产"),
        # registered 态也计入包计数（registered 口径＝全量，含 verified）
        make_record("retail", code="CN_RETAIL", domain="国内贸易", status="registered"),
        make_record("city-gdp", code="CITY_GDP", domain="city"),
        make_record("city-pop", code="CITY_POP", domain="city"),
        make_record("city-unemp", code="CITY_UNEMP", domain="city"),
        make_record("city-hp", code="CITY_HP", domain="城市指标"),
        make_record("city-den", code="CITY_DEN", domain="城市指标"),
    ]
    # source_db 切片（indicator_id 前半边即 source_db）
    records += [make_record(f"yearbook_catalog:YB-{n:03d}") for n in range(1, 8)]
    records += [
        make_record("fd_open_data:OD-001", domain="技术指标"),
        make_record("fd_open_data:OD-002", domain="指数行情"),
        make_record("fd_open_data:OD-003", domain="中国宏观"),
        make_record("fd_open_data:OD-004", domain="世界银行"),
        make_record("fd_open_data:OD-005", domain="PMI"),  # 行业景气跨源重叠
        make_record("fd_open_data:OD-006", domain="就业"),  # 人口就业跨源重叠
    ]
    return records


@pytest.fixture()
def packs_env(tmp_path, monkeypatch):
    """隔离环境：目录镜像临时库 + 平台库（客户/订阅）+ 挂好路由的应用。"""
    # 1) 目录镜像：独立临时库（假权威源 + internal 同步落 seed）
    monkeypatch.setattr(catalog_settings, "fd_wire_database_url", None)
    monkeypatch.setattr(catalog_settings, "catalog_database_url", f"sqlite:///{tmp_path/'packs_mirror.db'}")
    monkeypatch.setattr(catalog_settings, "database_url", None)
    monkeypatch.setattr(catalog_settings, "catalog_internal_token", "test-internal-token")
    monkeypatch.setattr(catalog_settings, "catalog_sync_enabled", False)
    monkeypatch.setattr(catalog_settings, "catalog_sync_on_startup", False)
    monkeypatch.setattr(catalog_settings, "catalog_sync_min_total", 0)
    reset_engines()
    init_catalog_db()

    # 2) 平台库：admin/billing/console（客户种子）+ pack_subscriptions 同库
    #    ——订阅引擎照生产语义跟随 FD_WIRE_DATABASE_URL 收敛开关
    platform = make_console_env(
        tmp_path,
        monkeypatch,
        customers=[(SUB_A, "cust-pack-a"), (SUB_B, "cust-pack-b")],
        db_filename="packs_platform.db",
    )
    monkeypatch.setenv("FD_WIRE_DATABASE_URL", platform.db_url)
    reset_packs_runtime()
    init_packs_db()

    # 3) 应用：catalog 公开/internal 路由 + packs 路由（假 JWKS 验签闭环）
    app = FastAPI()
    register_catalog(app)
    app.include_router(packs_router)
    fake = FakeRegistryClient(_seed_records())
    app.dependency_overrides[get_registry_client] = lambda: fake
    install_fake_jwks(monkeypatch)
    client = TestClient(app)
    resp = client.post("/internal/catalog/sync", headers=TOKEN_HEADER)
    assert resp.status_code == 200, resp.text

    yield SimpleNamespace(
        app=app,
        client=client,
        fake=fake,
        headers_a=bearer(mint_id_token(SUB_A)),
        headers_b=bearer(mint_id_token(SUB_B)),
    )
    reset_engines()
    reset_packs_runtime()


# ------------------------------------------------- 公开列表（2.1，不登录）


def test_list_packs_public_anonymous_with_counts(packs_env) -> None:
    resp = packs_env.client.get("/api/v1/packs")
    assert resp.status_code == 200
    body = resp.json()
    assert [p["id"] for p in body] == [p.id for p in PACKS]  # 8 包、展示序
    counts = {p["id"]: p["indicator_count"] for p in body}
    assert counts == EXPECTED_COUNTS
    # 列表项字段面：双语名称/描述/指标数（不含内部 filter 细节）
    assert set(body[0]) == {"id", "name_zh", "name_en", "desc", "indicator_count"}
    assert body[0]["name_zh"] and body[0]["name_en"] and body[0]["desc"]


def test_list_counts_follow_mirror_resync(packs_env) -> None:
    """目录即真相：权威源增删后包计数随下一次镜像同步更新。"""
    packs_env.fake.upsert_authority(
        make_record("fd_open_data:OD-007", domain="价格指数")
    )
    packs_env.fake.remove_authority("fd_open_data:OD-001")
    resp = packs_env.client.post("/internal/catalog/sync", headers=TOKEN_HEADER)
    assert resp.status_code == 200
    counts = {p["id"]: p["indicator_count"] for p in packs_env.client.get("/api/v1/packs").json()}
    assert counts["macro-prices"] == EXPECTED_COUNTS["macro-prices"] + 1
    assert counts["open-data-panorama"] == EXPECTED_COUNTS["open-data-panorama"]  # 增删相抵


# ------------------------------------------------- 公开详情（2.1，分页）


def test_pack_detail_default_pagination_and_fields(packs_env) -> None:
    resp = packs_env.client.get("/api/v1/packs/money-finance")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "money-finance"
    assert (body["page"], body["page_size"], body["total"]) == (1, 20, 4)
    assert body["indicator_count"] == 4
    assert set(body["items"][0]) == {"code", "name_zh", "domain"}
    assert {i["code"] for i in body["items"]} == {"CN_M2", "CN_RRR", "CN_LPR", "BTC_USD"}
    # 维度内取并集：三个域的行都在
    assert {i["domain"] for i in body["items"]} == {"货币金融", "利率", "加密货币"}


def test_pack_detail_pagination_walks_pages(packs_env) -> None:
    base = "/api/v1/packs/industry-activity"
    full = packs_env.client.get(base, params={"page_size": 100}).json()
    assert full["total"] == 6

    p1 = packs_env.client.get(base, params={"page": 1, "page_size": 4}).json()
    p2 = packs_env.client.get(base, params={"page": 2, "page_size": 4}).json()
    p3 = packs_env.client.get(base, params={"page": 3, "page_size": 4}).json()
    assert len(p1["items"]) == 4 and len(p2["items"]) == 2 and p3["items"] == []
    # 分页不重不漏（排序稳定：domain, code）
    codes = [i["code"] for i in p1["items"]] + [i["code"] for i in p2["items"]]
    assert sorted(codes) == sorted(i["code"] for i in full["items"])
    # 越界分页参数被校验拒绝（与目录 /indicators 同界）
    assert packs_env.client.get(base, params={"page": 0}).status_code == 422
    assert packs_env.client.get(base, params={"page_size": 101}).status_code == 422


def test_pack_detail_source_db_filters_and_overlap(packs_env) -> None:
    """source_db 切片包 + 跨源重叠：同一行可同时计入 domain 包与源全景包。"""
    yearbook = packs_env.client.get("/api/v1/packs/yearbook-panorama").json()
    assert yearbook["total"] == 7
    assert all(i["domain"] == "macro" for i in yearbook["items"])

    od = packs_env.client.get("/api/v1/packs/open-data-panorama").json()
    assert od["total"] == 6
    by_domain = {i["domain"] for i in od["items"]}
    assert "就业" in by_domain and "PMI" in by_domain  # 重叠行在源全景里如实出现


def test_pack_detail_unknown_404(packs_env) -> None:
    assert packs_env.client.get("/api/v1/packs/nope").status_code == 404


# ------------------------------------------------- 订阅面（2.2，登录）


def test_subscription_endpoints_require_auth(packs_env) -> None:
    assert packs_env.client.get("/api/v1/packs/subscriptions").status_code == 401
    assert (
        packs_env.client.post("/api/v1/packs/macro-prices/subscribe").status_code == 401
    )
    assert (
        packs_env.client.delete("/api/v1/packs/macro-prices/subscribe").status_code == 401
    )


def test_subscribe_idempotent_and_isolated(packs_env) -> None:
    client = packs_env.client
    # 订阅 → 重复订阅幂等 200，且不多写行
    first = client.post(
        "/api/v1/packs/macro-prices/subscribe", headers=packs_env.headers_a
    )
    assert first.status_code == 200 and first.json() == {
        "pack_id": "macro-prices",
        "subscribed": True,
    }
    again = client.post(
        "/api/v1/packs/macro-prices/subscribe", headers=packs_env.headers_a
    )
    assert again.status_code == 200 and again.json()["subscribed"] is True
    assert client.get(
        "/api/v1/packs/subscriptions", headers=packs_env.headers_a
    ).json() == ["macro-prices"]  # 幂等：仍只有一条

    # 跨客户隔离：B 看不到 A 的订阅；B 订另一包互不影响
    assert client.get(
        "/api/v1/packs/subscriptions", headers=packs_env.headers_b
    ).json() == []
    client.post("/api/v1/packs/money-finance/subscribe", headers=packs_env.headers_b)
    assert client.get(
        "/api/v1/packs/subscriptions", headers=packs_env.headers_a
    ).json() == ["macro-prices"]
    assert client.get(
        "/api/v1/packs/subscriptions", headers=packs_env.headers_b
    ).json() == ["money-finance"]

    # 退订幂等（DELETE /subscribe 与 POST /unsubscribe 两形态等价）
    unsub = client.delete(
        "/api/v1/packs/macro-prices/subscribe", headers=packs_env.headers_a
    )
    assert unsub.status_code == 200 and unsub.json()["subscribed"] is False
    unsub_again = client.post(
        "/api/v1/packs/macro-prices/unsubscribe", headers=packs_env.headers_a
    )
    assert unsub_again.status_code == 200 and unsub_again.json()["subscribed"] is False
    assert client.get(
        "/api/v1/packs/subscriptions", headers=packs_env.headers_a
    ).json() == []
    # B 不受 A 退订影响
    assert client.get(
        "/api/v1/packs/subscriptions", headers=packs_env.headers_b
    ).json() == ["money-finance"]


def test_unknown_pack_subscribe_unsubscribe_404(packs_env) -> None:
    for method, path in (
        ("post", "/api/v1/packs/nope/subscribe"),
        ("delete", "/api/v1/packs/nope/subscribe"),
        ("post", "/api/v1/packs/nope/unsubscribe"),
        ("delete", "/api/v1/packs/nope/unsubscribe"),
    ):
        resp = getattr(packs_env.client, method)(path, headers=packs_env.headers_a)
        assert resp.status_code == 404, f"{method.upper()} {path}"


def test_subscription_requires_mapped_customer(packs_env) -> None:
    """有效 token 但 sub 未映射客户（未首登开户）→ 401。"""
    stranger = bearer(mint_id_token("sub-stranger-unmapped"))
    resp = packs_env.client.get("/api/v1/packs/subscriptions", headers=stranger)
    assert resp.status_code == 401


# ------------------------------------------------- filter codes 分支（1.1）


def test_filter_codes_branch_via_service(packs_env) -> None:
    """filter.codes 精确圈定语义码（首批包未用，服务层直测保分支）。"""
    pack = PackDefinition(
        id="codes-test",
        name_zh="码集测试",
        name_en="Codes Test",
        desc="测试专用",
        filter={"codes": ["CN_CPI_YOY", "CN_EXPORT", "NO_SUCH_CODE"]},
    )
    service = PackQueryService()
    assert service.count(pack) == 2
    result = service.indicators(pack)
    assert {i["code"] for i in result["items"]} == {"CN_CPI_YOY", "CN_EXPORT"}


# ------------------------------------------------- 迁移链 0022（1.2）


@pytest.fixture()
def migrated_0022(tmp_path, monkeypatch):
    """对临时库跑 alembic upgrade 至 0022（链首 0001 起，全链真实迁移）。"""
    monkeypatch.delenv("FD_WIRE_DATABASE_URL", raising=False)
    db_url = f"sqlite:///{tmp_path}/packs_migration.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    cfg = Config(str(API_DIR / "alembic.ini"))
    command.upgrade(cfg, "0022_pack_subscriptions")
    return cfg, db_url


def test_migration_creates_pack_subscriptions(migrated_0022) -> None:
    """0022 建表 + customer_id 索引 + (customer_id, pack_id) 唯一；幂等重跑无恙。"""
    from sqlalchemy import create_engine, inspect

    cfg, db_url = migrated_0022
    command.upgrade(cfg, "0022_pack_subscriptions")  # 幂等重跑

    engine = create_engine(db_url)
    inspector = inspect(engine)
    assert "pack_subscriptions" in inspector.get_table_names()
    assert {c["name"] for c in inspector.get_columns("pack_subscriptions")} == {
        "id", "customer_id", "pack_id", "created_at",
    }
    assert {ix["name"] for ix in inspector.get_indexes("pack_subscriptions")} >= {
        "ix_pack_subscriptions_customer_id",
    }
    # (customer_id, pack_id) 唯一（sqlite autoindex，PRAGMA 按列断言）
    with engine.connect() as conn:
        unique_sets = []
        for _, name, unique, *_ in conn.exec_driver_sql(
            "PRAGMA index_list(pack_subscriptions)"
        ).fetchall():
            if not unique:
                continue
            cols = conn.exec_driver_sql(f"PRAGMA index_info({name})").fetchall()
            unique_sets.append({row[2] for row in cols})
    assert {"customer_id", "pack_id"} in unique_sets
    engine.dispose()


def test_migration_schema_matches_orm(migrated_0022) -> None:
    """drift check：迁移产出列集与 ORM 声明（PacksBase.metadata）一致。"""
    from sqlalchemy import create_engine, inspect

    from fd_wire_api.packs.db import PacksBase

    _, db_url = migrated_0022
    engine = create_engine(db_url)
    inspector = inspect(engine)
    migrated = {c["name"] for c in inspector.get_columns("pack_subscriptions")}
    declared = set(PacksBase.metadata.tables["pack_subscriptions"].columns.keys())
    assert migrated == declared, f"pack_subscriptions 迁移与 ORM 列漂移: {migrated ^ declared}"
    engine.dispose()


def test_migration_downgrade_drops_table(migrated_0022) -> None:
    from sqlalchemy import create_engine, inspect

    cfg, db_url = migrated_0022
    command.downgrade(cfg, "0021_artifact_lifecycle")
    engine = create_engine(db_url)
    assert "pack_subscriptions" not in inspect(engine).get_table_names()
    engine.dispose()
