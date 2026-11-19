"""资产目录 API 测试（wire-asset-catalog 任务组 1/2 + 任务 6.1 红线）。

覆盖：
- 元数据合并视图：缺行 GET 全默认值（LEFT JOIN + COALESCE 语义）且**不落
  行**（懒初始化——首次 PATCH 才创建，design D1）；
- 敏感度三级：常规/内部/敏感 通过；「机密」「公开」等 422 且 detail 带
  合法清单（design D3）；
- 团队角色门禁：owner/editor 读+写 200、viewer 读 200 写 403（六格矩阵）；
- 留痕：每次 PATCH append 一条（前后六字段快照、修改人 sub、新→旧序），
  事件端点可查、他企/不存在 404（design D7）；
- 目录浏览：域（含「未分类」哨兵）/标签/敏感度/来源类型/新鲜度组合筛选
  + 名称与描述搜索 + 分页；未补全条目按默认值参与筛选；
- 新鲜度钩子：quality.service.freshness_statuses 在场时按其判定过滤、
  缺席（ImportError）时全员按 unknown（诚实降级不误判）；
- cadence_effective：api 层数据集空节奏时查指标注册表（经 catalog 客户端）
  、查到即回显但**绝不写回**；权威源不可达 → null；非 api 层不查；
- 客户隔离：他企关键字零命中、他企 dataset_id 一律 404（不泄露）；
- 未鉴权：资产目录/元数据/留痕端点无凭证一律 401（任务 2.4）；
- 红线 3（任务 6.1）：公开目录搜索客户资产的自定义标签/描述关键字零
  命中（隔离镜像库 + 无关指标对照，证明搜索面活着只是不泄漏）。

环境：datasets 内存库（含资产两表——挂 DatasetsBase）+ 平台库
（admin/billing/console 同一临时文件库，console_fixture；owner/editor/
viewer 三角色种子）；注册表节奏查询默认替换为无网络假件（真实现的单测
用 fake HttpRegistryClient 走真路径）。
"""

from __future__ import annotations

import os
import sys
import types
from datetime import datetime, timedelta, timezone

# 在导入 app 前把默认引擎指向内存库（TestClient 启动钩子不落盘）。
os.environ.setdefault("AI_DATABASE_URL", "sqlite://")
os.environ.setdefault("FD_WIRE_DATASETS_DATABASE_URL", "sqlite://")
os.environ.setdefault("FD_WIRE_ENTITIES_DATABASE_URL", "sqlite://")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# 先经 app 入口导入（照 test_team 先例）：app.py 内 ai 先于 datasets 初始化，
# 既有 datasets↔ai 环从本侧进入会撞部分初始化模块。
from fd_wire_api.app import app

import fd_wire_api.assets.routes as assets_routes
from fd_wire_api.assets.models import AssetMeta, SENSITIVITY_LEVELS
from fd_wire_api.catalog.client import ConceptBatch, ConceptRecord
from fd_wire_api.console.models import CustomerMember
from fd_wire_api.datasets.db import DatasetsBase, get_db as get_datasets_db
from fd_wire_api.datasets.models import Dataset

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

# 真实现的节奏查询（夹具默认替换为无网络假件；个别用例恢复真实现走 fake 客户端）
_REAL_CADENCE_LOOKUP = assets_routes._lookup_registry_cadence

SUB_OWNER = "sub-asset-owner"
SUB_OTHER = "sub-asset-other"
SUB_EDITOR = "sub-asset-editor"
SUB_VIEWER = "sub-asset-viewer"

CUST = "asset-cust"  # Customer.name = 客户面命名空间键（datasets 域）
OTHER_CUST = "asset-other"

OWNER = bearer(mint_id_token(SUB_OWNER))
OTHER = bearer(mint_id_token(SUB_OTHER))
EDITOR = bearer(mint_id_token(SUB_EDITOR))
VIEWER = bearer(mint_id_token(SUB_VIEWER))


@pytest.fixture()
def db_session():
    """datasets 内存库（含资产两表——assets.models 挂 DatasetsBase）。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    DatasetsBase.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(monkeypatch, tmp_path, db_session):
    """datasets 库会话覆写 + 三角色种子 + 注册表查询无网络假件的 TestClient。"""
    from fd_wire_api.catalog import catalog_settings

    monkeypatch.setattr(catalog_settings, "catalog_sync_enabled", False)
    # 节奏查询默认无网络（个别用例显式恢复真实现配 fake 客户端）
    monkeypatch.setattr(
        assets_routes, "_lookup_registry_cadence", lambda dataset_key: None
    )

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    install_fake_jwks(monkeypatch)
    env = make_console_env(
        tmp_path,
        monkeypatch,
        customers=[(SUB_OWNER, CUST), (SUB_OTHER, OTHER_CUST)],
    )
    with env.session() as sess:  # 团队成员：editor/viewer 挂本客户（三角角色）
        sess.add(
            CustomerMember(
                customer_id=env.subs[SUB_OWNER], logto_sub=SUB_EDITOR, role="editor"
            )
        )
        sess.add(
            CustomerMember(
                customer_id=env.subs[SUB_OWNER], logto_sub=SUB_VIEWER, role="viewer"
            )
        )
        sess.commit()

    app.dependency_overrides[get_datasets_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# ------------------------------------------------------------------ 助手


def seed_dataset(
    db_session,
    *,
    customer_id: str = CUST,
    key: str,
    name: str | None = None,
    source_kind: str = "upload",
    tier: str = "api",
    row_count: int = 10,
    created_at: datetime | None = None,
) -> str:
    """直插一条数据集登记，返回 dataset id（created_at 可控保证列表序稳定）。"""
    dataset = Dataset(
        customer_id=customer_id,
        key=key,
        name=name or f"{key} 数据源",
        source_kind=source_kind,
        storage_tier=tier,
        columns_json=[{"name": "id", "type": "TEXT"}],
        row_count=row_count,
        created_at=created_at or datetime(2026, 1, 1, 12, 0, 0),
    )
    db_session.add(dataset)
    db_session.commit()
    return dataset.id


def patch_meta(client, dataset_id: str, payload: dict, headers: dict = OWNER):
    return client.patch(
        f"/api/v1/datasets/{dataset_id}/asset-meta", json=payload, headers=headers
    )


# ------------------------------------------------- 元数据读/写（任务 1.1/1.2）


def test_get_defaults_on_missing_row_and_lazy_create_on_patch(client, db_session):
    """未补全：GET 全默认值且不落行；首次 PATCH 才落行（懒初始化 D1）。"""
    ds = seed_dataset(db_session, key="orders")
    url = f"/api/v1/datasets/{ds}/asset-meta"

    resp = client.get(url, headers=OWNER)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "dataset_id": ds,
        "domain": "",
        "tags": [],
        "description": "",
        "cadence_hint": None,
        "owner_note": "",
        "sensitivity": "常规",
        "updated_by": None,
        "updated_at": None,
        "cadence_effective": None,
        # 有效敏感度三字段（wire-sensitivity-propagation）：无祖先时 level=自身
        "sensitivity_effective": "常规",
        "sensitivity_inherited": False,
        "sensitivity_incomplete": False,
    }
    assert db_session.get(AssetMeta, ds) is None  # GET 绝不创建行

    patched = patch_meta(
        client, ds, {"tags": ["销售", "日报"], "description": "订单事实表"}
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["tags"] == ["销售", "日报"]
    assert body["description"] == "订单事实表"
    assert body["sensitivity"] == "常规"  # 未提供的字段保持默认
    assert body["updated_by"] == SUB_OWNER
    assert body["updated_at"] is not None
    assert db_session.get(AssetMeta, ds) is not None  # PATCH 才落行

    # 部分更新：只动提供的字段（cadence_hint 显式 null = 清空回「未知」）
    assert patch_meta(client, ds, {"cadence_hint": "daily"}).json()["cadence_hint"] == "daily"
    cleared = patch_meta(client, ds, {"cadence_hint": None})
    assert cleared.json()["cadence_hint"] is None
    assert cleared.json()["tags"] == ["销售", "日报"]  # 其余不受影响

    # 未知/他企数据集一律 404（不泄露）
    assert client.get("/api/v1/datasets/ds-ghost/asset-meta", headers=OWNER).status_code == 404


def test_tags_cleaned_deduped(client, db_session):
    """标签清洗：去空白、去空项、保序去重。"""
    ds = seed_dataset(db_session, key="clean_tags")
    resp = patch_meta(client, ds, {"tags": [" 销售 ", "", "销售", "日报", "  "]})
    assert resp.status_code == 200
    assert resp.json()["tags"] == ["销售", "日报"]


# ------------------------------------------------- 敏感度三级（任务 1.3 / D3）


def test_sensitivity_enum_accept_and_reject(client, db_session):
    """三合法值通过；「机密」「公开」等 422 且 detail 带合法清单。"""
    ds = seed_dataset(db_session, key="sensitivity_probe")
    for level in SENSITIVITY_LEVELS:  # 常规 / 内部 / 敏感
        resp = patch_meta(client, ds, {"sensitivity": level})
        assert resp.status_code == 200, resp.text
        assert resp.json()["sensitivity"] == level
    for bad in ("机密", "公开", "public", ""):
        resp = patch_meta(client, ds, {"sensitivity": bad})
        assert resp.status_code == 422, bad
        detail = resp.json()["detail"]
        for level in SENSITIVITY_LEVELS:
            assert level in detail
    # 拒绝后保持最后合法值（零半写）
    assert client.get(f"/api/v1/datasets/{ds}/asset-meta", headers=OWNER).json()[
        "sensitivity"
    ] == "敏感"


def test_sensitivity_filter_rejects_unknown_value(client):
    """目录筛选的敏感度参数同样只认三级（422 带清单）。"""
    resp = client.get("/api/v1/assets", params={"sensitivity": "机密"}, headers=OWNER)
    assert resp.status_code == 422
    assert "常规" in resp.json()["detail"] and "敏感" in resp.json()["detail"]


# ------------------------------------------------- 角色门禁（任务 1.4）


def test_role_matrix_owner_editor_write_viewer_readonly(client, db_session):
    """三角色 × 读/写 六格：owner/editor 读+写 200；viewer 读 200 写 403。"""
    ds = seed_dataset(db_session, key="gated")
    url = f"/api/v1/datasets/{ds}/asset-meta"

    # owner 读/写
    assert client.get(url, headers=OWNER).status_code == 200
    assert patch_meta(client, ds, {"domain": "销售"}, headers=OWNER).status_code == 200
    # editor 读/写
    assert client.get(url, headers=EDITOR).status_code == 200
    assert patch_meta(client, ds, {"owner_note": "editor 补全"}, headers=EDITOR).status_code == 200
    # viewer 读（含目录与留痕）/写 403
    assert client.get("/api/v1/assets", headers=VIEWER).status_code == 200
    assert client.get(url, headers=VIEWER).status_code == 200
    assert client.get(f"{url}/events", headers=VIEWER).status_code == 200
    denied = patch_meta(client, ds, {"domain": "越权"}, headers=VIEWER)
    assert denied.status_code == 403
    assert "权限不足" in denied.json()["detail"]
    # viewer 的 403 是零半写：值保持 editor 的成功态
    assert client.get(url, headers=VIEWER).json()["owner_note"] == "editor 补全"


# ------------------------------------------------- 留痕（任务 1.5 / D7）


def test_events_appended_and_queryable(client, db_session):
    """每次修改 append 一条：前后六字段快照 + 修改人 + 新→旧序。"""
    ds = seed_dataset(db_session, key="audited")
    url = f"/api/v1/datasets/{ds}/asset-meta"

    assert patch_meta(client, ds, {"tags": ["a"]}, headers=OWNER).status_code == 200
    assert patch_meta(client, ds, {"sensitivity": "内部"}, headers=EDITOR).status_code == 200

    events = client.get(f"{url}/events", headers=OWNER).json()["events"]
    assert len(events) == 2
    # 新 → 旧
    assert events[0]["actor_sub"] == SUB_EDITOR
    assert events[1]["actor_sub"] == SUB_OWNER
    assert events[0]["id"] > events[1]["id"]
    assert events[0]["at"] is not None
    # 首次修改的 before = 全默认值快照（COALESCE 语义同源）
    assert events[1]["before"] == {
        "domain": "",
        "tags": [],
        "description": "",
        "cadence_hint": None,
        "owner_note": "",
        "sensitivity": "常规",
    }
    assert events[1]["after"]["tags"] == ["a"]
    # 敏感度变更与其它修改同一留痕（前后值可查）
    assert events[0]["before"]["sensitivity"] == "常规"
    assert events[0]["after"]["sensitivity"] == "内部"

    # limit 收窄（最近 1 条）
    only_latest = client.get(f"{url}/events", params={"limit": 1}, headers=OWNER)
    assert len(only_latest.json()["events"]) == 1
    assert only_latest.json()["events"][0]["actor_sub"] == SUB_EDITOR
    # 未知数据集 404
    assert (
        client.get("/api/v1/datasets/ds-ghost/asset-meta/events", headers=OWNER).status_code
        == 404
    )


# ------------------------------------------------- cadence_effective（任务 2.2 / D4）


def test_cadence_effective_registry_lookup_no_writeback(client, db_session, monkeypatch):
    """api 层空节奏 → 查注册表回显 cadence_effective，绝不写回 cadence_hint。"""
    api_ds = seed_dataset(db_session, key="cpi_series", tier="api")
    cell_ds = seed_dataset(db_session, key="orders_cell", tier="cell")
    calls: list[str] = []

    def fake_lookup(dataset_key: str):
        calls.append(dataset_key)
        return "weekly" if dataset_key == "cpi_series" else None

    monkeypatch.setattr(assets_routes, "_lookup_registry_cadence", fake_lookup)

    resp = client.get(f"/api/v1/datasets/{api_ds}/asset-meta", headers=OWNER)
    assert resp.status_code == 200
    body = resp.json()
    assert body["cadence_hint"] is None  # 未写回
    assert body["cadence_effective"] == "weekly"
    assert db_session.get(AssetMeta, api_ds) is None  # 也没落元数据行

    # 显式 cadence_hint 优先于注册表推断（不再查）
    assert patch_meta(client, api_ds, {"cadence_hint": "monthly"}).json()[
        "cadence_effective"
    ] == "monthly"
    # 非 api 层数据集空节奏 → 不查注册表、null（未知）
    cell_body = client.get(f"/api/v1/datasets/{cell_ds}/asset-meta", headers=OWNER).json()
    assert cell_body["cadence_effective"] is None
    assert calls == ["cpi_series"]


def test_cadence_lookup_real_helper_with_fake_client(client, db_session, monkeypatch):
    """真 _lookup_registry_cadence + fake HttpRegistryClient：按语义码/原生码
    命中并归一 frequency；权威源不可达 → null（不抛错）。"""
    import fd_wire_api.catalog.client as catalog_client

    ds = seed_dataset(db_session, key="cn_cpi", tier="api")
    monkeypatch.setattr(assets_routes, "_lookup_registry_cadence", _REAL_CADENCE_LOOKUP)
    assets_routes._registry_cadence_cache.clear()

    record = ConceptRecord(
        indicator_id="gta:cpi", code="cn_cpi", native_code="cpi", frequency="月度"
    )

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def fetch_concepts(self) -> ConceptBatch:
            return ConceptBatch(items=[record], full=True)

    monkeypatch.setattr(catalog_client, "HttpRegistryClient", FakeClient)
    body = client.get(f"/api/v1/datasets/{ds}/asset-meta", headers=OWNER).json()
    assert body["cadence_effective"] == "monthly"  # 「月度」→ monthly
    assert body["cadence_hint"] is None  # 仍不写回

    # 权威源不可达：客户端抛错 → 如实 null（GET 不失败）
    class BoomClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def fetch_concepts(self) -> ConceptBatch:
            raise RuntimeError("registry unreachable (simulated)")

    monkeypatch.setattr(catalog_client, "HttpRegistryClient", BoomClient)
    assets_routes._registry_cadence_cache.clear()
    other = seed_dataset(db_session, key="gone_series", tier="api")
    resp = client.get(f"/api/v1/datasets/{other}/asset-meta", headers=OWNER)
    assert resp.status_code == 200
    assert resp.json()["cadence_effective"] is None


# ------------------------------------------------- 目录浏览与筛选（任务 2.1）


@pytest.fixture()
def catalog_seed(db_session):
    """标准三件套（本客户）+ 他企一件：

    - sales_daily：域=销售、标签[销售,日报]、敏感度=内部、描述「每日销售汇总」
    - sales_weekly：域=销售、标签[销售]、敏感度缺省常规、派生 cell 层
    - hr_roster：未补全（域=未分类、敏感度按默认常规参与筛选）
    - secret_vault：他企（隔离对照）
    """
    t0 = datetime(2026, 1, 1, 12, 0, 0)
    sales_daily = seed_dataset(
        db_session,
        key="sales_daily",
        name="销售日报",
        source_kind="upload",
        tier="api",
        created_at=t0 + timedelta(minutes=30),
    )
    sales_weekly = seed_dataset(
        db_session,
        key="sales_weekly",
        name="销售周报",
        source_kind="derived",
        tier="cell",
        created_at=t0 + timedelta(minutes=20),
    )
    hr_roster = seed_dataset(
        db_session,
        key="hr_roster",
        name="员工花名册",
        source_kind="connector",
        tier="cell",
        created_at=t0 + timedelta(minutes=10),
    )
    secret_vault = seed_dataset(
        db_session,
        customer_id=OTHER_CUST,
        key="secret_vault",
        name="绝密金库",
        created_at=t0 + timedelta(minutes=5),
    )
    assert patch_meta_db(db_session, sales_daily, domain="销售", tags=["销售", "日报"], sensitivity="内部", description="每日销售汇总")
    assert patch_meta_db(db_session, sales_weekly, domain="销售", tags=["销售"])
    return {
        "sales_daily": sales_daily,
        "sales_weekly": sales_weekly,
        "hr_roster": hr_roster,
        "secret_vault": secret_vault,
    }


def patch_meta_db(db_session, dataset_id: str, **fields) -> AssetMeta:
    """直插/直改元数据行（不经端点——种子用）。"""
    meta = db_session.get(AssetMeta, dataset_id) or AssetMeta(dataset_id=dataset_id)
    db_session.add(meta)
    for name, value in fields.items():
        if name == "tags":
            meta.tags_json = value
        else:
            setattr(meta, name, value)
    db_session.commit()
    return meta


def _list_ids(client, **params) -> list[str]:
    resp = client.get("/api/v1/assets", params=params, headers=OWNER)
    assert resp.status_code == 200, resp.text
    return [item["id"] for item in resp.json()["items"]]


def test_catalog_combined_filters_and_unclassified(client, db_session, catalog_seed):
    """域×敏感度组合筛选；未补全条目归「未分类」可筛（默认值参与）。"""
    s = catalog_seed
    assert _list_ids(client, domain="销售") == [s["sales_daily"], s["sales_weekly"]]
    assert _list_ids(client, domain="未分类") == [s["hr_roster"]]
    assert _list_ids(client, sensitivity="内部") == [s["sales_daily"]]
    # 组合：域=销售 且 敏感度=内部 → 仅日报
    assert _list_ids(client, domain="销售", sensitivity="内部") == [s["sales_daily"]]
    # 敏感度=常规含未补全条目（缺行按默认值参与）
    assert set(_list_ids(client, sensitivity="常规")) == {s["sales_weekly"], s["hr_roster"]}
    assert _list_ids(client, tag="日报") == [s["sales_daily"]]
    assert _list_ids(client, tag="不存在标签") == []
    assert _list_ids(client, source_kind="connector") == [s["hr_roster"]]
    assert _list_ids(client, source_kind="derived") == [s["sales_weekly"]]


def test_catalog_q_search_and_isolation(client, db_session, catalog_seed):
    """q 命中名称或描述；他企条目与关键字互不可见（隔离不受筛选影响）。"""
    s = catalog_seed
    assert _list_ids(client, q="花名册") == [s["hr_roster"]]  # 名称命中
    assert _list_ids(client, q="销售汇总") == [s["sales_daily"]]  # 描述命中
    assert _list_ids(client, q="销售") == [s["sales_daily"], s["sales_weekly"]]
    # 他企专属关键字：本客户零命中
    assert _list_ids(client, q="绝密金库") == []
    assert _list_ids(client, q="金库", domain="销售") == []

    # 他企视角：只见自己的，本企关键字零命中
    other_resp = client.get("/api/v1/assets", params={"q": "销售"}, headers=OTHER)
    assert other_resp.status_code == 200
    assert other_resp.json()["total"] == 0
    other_all = client.get("/api/v1/assets", headers=OTHER).json()
    assert [item["id"] for item in other_all["items"]] == [s["secret_vault"]]
    assert other_all["total"] == 1

    # 他企 dataset_id 一律 404（读/写/留痕三面，不泄露存在性）
    for method, path in (
        ("get", f"/api/v1/datasets/{s['secret_vault']}/asset-meta"),
        ("patch", f"/api/v1/datasets/{s['secret_vault']}/asset-meta"),
        ("get", f"/api/v1/datasets/{s['secret_vault']}/asset-meta/events"),
    ):
        kwargs = {"json": {"domain": "劫持"}} if method == "patch" else {}
        resp = getattr(client, method)(path, headers=OWNER, **kwargs)
        assert resp.status_code == 404, (method, resp.status_code)


def test_catalog_pagination(client, db_session, catalog_seed):
    """分页：total 全量、页大小切分、页间不重不漏（创建序新→旧稳定）。"""
    s = catalog_seed
    page1 = client.get(
        "/api/v1/assets", params={"page": 1, "page_size": 2}, headers=OWNER
    ).json()
    page2 = client.get(
        "/api/v1/assets", params={"page": 2, "page_size": 2}, headers=OWNER
    ).json()
    assert page1["total"] == 3 and page2["total"] == 3
    assert [i["id"] for i in page1["items"]] == [s["sales_daily"], s["sales_weekly"]]
    assert [i["id"] for i in page2["items"]] == [s["hr_roster"]]
    assert page1["page"] == 1 and page1["page_size"] == 2


def test_catalog_item_shape(client, db_session, catalog_seed):
    """列表项 = 数据集摘要平铺 + 嵌套 asset 段（六字段快照 + cadence_effective）。"""
    s = catalog_seed
    items = {
        item["id"]: item
        for item in client.get("/api/v1/assets", headers=OWNER).json()["items"]
    }
    daily = items[s["sales_daily"]]
    # 条目顶层只保留数据集摘要 + 修改人/时间（元数据不再平铺）
    assert set(daily.keys()) == {
        "id",
        "name",
        "source_kind",
        "row_count",
        "storage_tier",
        "created_at",
        "updated_by",
        "updated_at",
        "asset",
    }
    assert daily["name"] == "销售日报"
    assert daily["source_kind"] == "upload"
    assert daily["storage_tier"] == "api"
    assert daily["row_count"] == 10
    assert daily["created_at"] is not None
    assert daily["asset"]["domain"] == "销售"
    assert daily["asset"]["tags"] == ["销售", "日报"]
    assert daily["asset"]["sensitivity"] == "内部"
    assert daily["asset"]["description"] == "每日销售汇总"
    assert daily["asset"]["cadence_hint"] is None
    assert daily["asset"]["owner_note"] == ""
    assert daily["asset"]["cadence_effective"] is None  # 列表不回查注册表
    # 未补全条目按默认值呈现（asset 段全默认）
    roster = items[s["hr_roster"]]
    assert roster["asset"]["domain"] == "" and roster["asset"]["tags"] == []
    assert roster["asset"]["sensitivity"] == "常规"
    assert roster["asset"]["cadence_effective"] is None
    assert roster["updated_by"] is None and roster["updated_at"] is None


# ------------------------------------------------- 新鲜度钩子（任务 2.1）


def test_freshness_filter_with_quality_hook(client, db_session, monkeypatch):
    """钩子在场：按 freshness_statuses 判定过滤；缺席：全员按 unknown。"""
    ds_fresh = seed_dataset(db_session, key="fresh_one")
    ds_stale = seed_dataset(db_session, key="stale_one")
    ds_unknown = seed_dataset(db_session, key="unk_one")

    fake_pkg = types.ModuleType("fd_wire_api.quality")
    fake_service = types.ModuleType("fd_wire_api.quality.service")

    def freshness_statuses(customer_id: str, dataset_ids: list[str]) -> dict[str, str]:
        assert customer_id == CUST
        assert set(dataset_ids) == {ds_fresh, ds_stale, ds_unknown}
        return {ds_fresh: "fresh", ds_stale: "stale"}

    fake_service.freshness_statuses = freshness_statuses
    fake_pkg.service = fake_service
    monkeypatch.setitem(sys.modules, "fd_wire_api.quality", fake_pkg)
    monkeypatch.setitem(sys.modules, "fd_wire_api.quality.service", fake_service)

    assert _list_ids(client, freshness="fresh") == [ds_fresh]
    assert _list_ids(client, freshness="stale") == [ds_stale]
    # 钩子没给的 → unknown
    assert _list_ids(client, freshness="unknown") == [ds_unknown]

    # 非法取值 → 422（Query pattern）
    assert (
        client.get("/api/v1/assets", params={"freshness": "rotten"}, headers=OWNER).status_code
        == 422
    )


def test_freshness_filter_importerror_fallback(client, db_session, monkeypatch):
    """钩子缺席（quality 切片未部署/滚动窗口）：ImportError → 全员 unknown，
    不误判。sys.modules 显式置 None 强制 ImportError（即使切片已落盘）。"""
    monkeypatch.setitem(sys.modules, "fd_wire_api.quality", None)
    # service 子模块也可能已被 app 导入缓存——一并置 None 强制 ImportError
    monkeypatch.setitem(sys.modules, "fd_wire_api.quality.service", None)
    t0 = datetime(2026, 1, 1, 12, 0, 0)
    ds_a = seed_dataset(db_session, key="any_a", created_at=t0)
    ds_b = seed_dataset(db_session, key="any_b", created_at=t0 + timedelta(minutes=1))
    assert _list_ids(client, freshness="unknown") == [ds_b, ds_a]  # 创建序新→旧
    assert _list_ids(client, freshness="fresh") == []


# ------------------------------------------------- 未鉴权（任务 2.4）


def test_unauthenticated_requests_get_401(client, db_session):
    """无凭证访问资产目录/元数据/留痕端点一律 401（不泄露任何资产信息）。"""
    ds = seed_dataset(db_session, key="locked")
    assert client.get("/api/v1/assets").status_code == 401
    assert client.get(f"/api/v1/datasets/{ds}/asset-meta").status_code == 401
    assert client.get(f"/api/v1/datasets/{ds}/asset-meta/events").status_code == 401
    resp = client.patch(f"/api/v1/datasets/{ds}/asset-meta", json={"domain": "越权"})
    assert resp.status_code == 401
    # 被拒后零半写
    assert db_session.get(AssetMeta, ds) is None


# ------------------------------------------------- 红线 3：公开面不泄漏（任务 6.1）


def test_public_catalog_never_returns_customer_asset_content(
    client, db_session, tmp_path, monkeypatch
):
    """公开目录搜索客户资产的自定义标签/描述关键字零命中（红线 3）。

    隔离镜像库 + 种一条无关公开指标作对照——证明搜索面活着、只是不含
    任何客户资产内容（标签/描述/数据集名全不命中）。"""
    from fd_wire_api.catalog import catalog_settings, init_db as init_catalog_mirror
    from fd_wire_api.catalog import reset_engines
    from fd_wire_api.catalog.db import sync_session
    from fd_wire_api.catalog.models import IndicatorSnapshot

    ds = seed_dataset(db_session, key="secret_sales", name="销售明细")
    tagged = patch_meta(
        client, ds, {"tags": ["绝密标签Q7"], "description": "内部绝密描述Z9"}
    )
    assert tagged.status_code == 200, tagged.text

    # 镜像库指向隔离临时库（不复用默认 ./fd_wire_dev.db）
    mirror_url = f"sqlite:///{tmp_path / 'catalog_mirror.db'}"
    monkeypatch.setattr(catalog_settings, "catalog_database_url", mirror_url)
    monkeypatch.setattr(catalog_settings, "database_url", None)
    monkeypatch.setattr(catalog_settings, "fd_wire_database_url", None)
    reset_engines()
    init_catalog_mirror()
    with sync_session() as sess:
        sess.add(
            IndicatorSnapshot(
                indicator_id="gta:cpi",
                code="CN_CPI",
                name_zh="居民消费价格指数",
                status="verified",
                synced_at=datetime.now(timezone.utc),
            )
        )
        sess.commit()

    # 客户资产的自定义标签/描述/数据集名 → 公开目录零命中（无需登录即可搜）
    for keyword in ("绝密标签Q7", "绝密描述Z9", "销售明细"):
        resp = client.get("/api/v1/catalog/indicators", params={"q": keyword})
        assert resp.status_code == 200
        assert resp.json()["total"] == 0
        assert resp.json()["items"] == []

    # 对照组：公开面自己的指标搜得到（搜索面非瘫，只是不泄漏）
    control = client.get("/api/v1/catalog/indicators", params={"q": "消费价格"})
    assert control.status_code == 200
    assert control.json()["total"] == 1
    assert control.json()["items"][0]["code"] == "CN_CPI"
