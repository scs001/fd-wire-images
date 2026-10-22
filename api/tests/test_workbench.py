"""工作台聚合端点测试（wire-personal-workbench 任务 1.3）。

覆盖：
- 聚合正确性：六段数字与各切片种子数据逐一对上（分层计数/五类产物/
  告警三口径/需求开放态与最新一单/免费配额真实数字）；
- 最近列表：updated_at（订阅 created_at）倒序 + ≤5 截断（三段）；
- 免费配额：免费档真实数字 / 付费档整段 null；
- 跨客户隔离（红线）：种客户 B 的数据集/产物/订阅/需求/告警事件，
  A 的响应全文断言 B 专属标识与计数零出现；
- 未登录 401；
- 全空客户：各段零值不炸（free_quota 如实 0/3/10000）。

harness 范式照 test_preferences / test_demand_auto_export：四切片各一
sqlite 内存库（StaticPool）+ ``app.dependency_overrides`` 覆盖四个
``get_db``（ai 一个引擎 create_all 覆盖 artifacts + alert_states +
alert_events 全部 AiBase 表）；平台库走 console_fixture 的
``install_fake_jwks`` + ``make_console_env``（B 客户切 basic 档，
照 test_datasets_free_tier.free_env 的切档手法）。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

# 在导入 app 前把惰性引擎的缺省库指向内存（防测试机落下 fd_wire_*.db
# 滞留文件；本文件实际走 get_db 覆写，四切片引擎零连接）。
os.environ.setdefault("AI_DATABASE_URL", "sqlite://")
_OWNS_FD_WIRE_URL = "FD_WIRE_DATABASE_URL" not in os.environ
os.environ.setdefault("FD_WIRE_DATABASE_URL", "sqlite://")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fd_wire_api.ai.db import AiBase, get_db as get_ai_db
from fd_wire_api.alerts.models import AlertEvent, AlertState
from fd_wire_api.app import app
from fd_wire_api.artifacts.models import Artifact
from fd_wire_api.datasets.db import DatasetsBase, get_db as get_datasets_db
from fd_wire_api.datasets.models import Dataset
from fd_wire_api.demands import get_db as get_demands_db
from fd_wire_api.demands.db import Base as DemandsBase
from fd_wire_api.demands.models import Demand
from fd_wire_api.packs.db import PacksBase, get_db as get_packs_db
from fd_wire_api.packs.definitions import PACKS, get_pack
from fd_wire_api.packs.models import PackSubscription

from console_fixture import bearer, install_fake_jwks, make_console_env, mint_id_token

if _OWNS_FD_WIRE_URL:
    del os.environ["FD_WIRE_DATABASE_URL"]

SUB_A = "sub-workbench-a"  # 免费档客户 A（主视角）
SUB_B = "sub-workbench-b"  # basic 档客户 B（隔离对端 + 付费配额对照）
SUB_C = "sub-workbench-c"  # 免费档全空客户 C（空态用例）

CUST_A = "workbench-cust-a"
CUST_B = "workbench-cust-b"
CUST_C = "workbench-cust-c"

HEADERS_A = bearer(mint_id_token(SUB_A))
HEADERS_B = bearer(mint_id_token(SUB_B))
HEADERS_C = bearer(mint_id_token(SUB_C))

URL = "/api/v1/workbench"

#: 冻结的 by_kind 八键展示序（展示面口径：与 web ARTIFACT_KINDS 一致，含
#: 表单构建器的 form、单记录公开页的 record-view——wire-record-view 任务
#: 1.3——与富文本文档的 doc——wire-rich-text-docs 任务 1.4；校验面五类
#: tuple 不覆盖后三者）
ALL_KINDS = [
    "dashboard", "form", "composite_indicator", "research_report",
    "alert_rule", "refresh_schedule", "record-view", "doc",
]

_BASE_TIME = datetime(2026, 10, 12, 8, 0, 0)


def at(minutes: float) -> datetime:
    """种子时间锚（整秒 naive UTC，避开 isoformat 微秒歧义）。"""
    return _BASE_TIME + timedelta(minutes=minutes)


def utc_now() -> datetime:
    """naive UTC 当前时刻（与 workbench 事件窗同一口径）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture()
def harness(tmp_path, monkeypatch):
    """四切片内存库 + 平台库（A 免费 / B basic / C 免费全空）。"""
    install_fake_jwks(monkeypatch)
    console_env = make_console_env(
        tmp_path,
        monkeypatch,
        customers=[(SUB_A, CUST_A), (SUB_B, CUST_B), (SUB_C, CUST_C)],
    )
    # B 切 basic 档（照 test_datasets_free_tier.free_env 的切档手法：
    # 平台库直改 plan_id）——隔离对端兼付费配额对照
    from fd_wire_api.billing.models import Customer, Plan

    with console_env.session() as sess:
        basic = sess.execute(select(Plan).where(Plan.code == "basic")).scalar_one()
        cust_b = sess.execute(
            select(Customer).where(Customer.name == CUST_B)
        ).scalar_one()
        cust_b.plan_id = basic.id
        sess.commit()

    engines = {}
    sessions = {}
    bases = {
        "datasets": DatasetsBase,
        "packs": PacksBase,
        "ai": AiBase,
        "demands": DemandsBase,
    }
    for name, base in bases.items():
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        base.metadata.create_all(bind=engine)
        engines[name] = engine
        sessions[name] = sessionmaker(
            bind=engine, autoflush=False, expire_on_commit=False
        )

    def override_for(name):
        def _override():
            db = sessions[name]()
            try:
                yield db
            finally:
                db.close()

        return _override

    app.dependency_overrides[get_packs_db] = override_for("packs")
    app.dependency_overrides[get_datasets_db] = override_for("datasets")
    app.dependency_overrides[get_ai_db] = override_for("ai")
    app.dependency_overrides[get_demands_db] = override_for("demands")

    client = TestClient(app)
    try:
        yield SimpleNamespace(client=client, db=sessions, platform=console_env)
    finally:
        app.dependency_overrides.clear()
        client.close()
        for engine in engines.values():
            engine.dispose()
        console_env.engine.dispose()


# ------------------------------------------------------------------ 种子


def seed_dataset(
    harness, *, customer_id: str = CUST_A, name: str, tier: str = "api",
    row_count: int = 0, size_bytes: int = 0, updated_at: datetime, key: str,
) -> str:
    with harness.db["datasets"]() as sess:
        dataset = Dataset(
            customer_id=customer_id,
            key=key,
            name=name,
            source_kind="upload",
            storage_tier=tier,
            row_count=row_count,
            size_bytes=size_bytes,
            updated_at=updated_at,
        )
        sess.add(dataset)
        sess.commit()
        return dataset.id


def seed_subscription(
    harness, *, customer_id: str = CUST_A, pack_id: str, created_at: datetime
) -> None:
    with harness.db["packs"]() as sess:
        sess.add(
            PackSubscription(customer_id=customer_id, pack_id=pack_id, created_at=created_at)
        )
        sess.commit()


def seed_artifact(
    harness, *, customer_id: str = CUST_A, kind: str = "dashboard",
    status: str = "published", title: str, updated_at: datetime,
) -> int:
    with harness.db["ai"]() as sess:
        artifact = Artifact(
            customer_id=customer_id, kind=kind, title=title,
            status=status, updated_at=updated_at,
        )
        sess.add(artifact)
        sess.commit()
        return artifact.id


def seed_alert_state(
    harness, *, artifact_id: int, customer_id: str, state: str,
    evaluated_at: datetime,
) -> None:
    with harness.db["ai"]() as sess:
        sess.add(
            AlertState(
                artifact_id=artifact_id,
                customer_id=customer_id,
                state=state,
                last_evaluated_at=evaluated_at,
            )
        )
        sess.commit()


def seed_alert_event(
    harness, *, artifact_id: int, customer_id: str, created_at: datetime,
    kind: str = "firing",
) -> None:
    with harness.db["ai"]() as sess:
        sess.add(
            AlertEvent(
                artifact_id=artifact_id,
                customer_id=customer_id,
                kind=kind,
                created_at=created_at,
            )
        )
        sess.commit()


def seed_demand(
    harness, *, code: str, customer_id: str = CUST_A, status: str = "submitted",
    updated_at: datetime,
) -> None:
    with harness.db["demands"]() as sess:
        sess.add(
            Demand(
                code=code,
                customer_id=customer_id,
                title=f"需求 {code}",
                intent="工作台测试需求",
                contact_email="a@example.com",
                status=status,
                updated_at=updated_at,
            )
        )
        sess.commit()


# ------------------------------------------------------------------ ①聚合正确性


def test_aggregate_matches_seed(harness) -> None:
    """六段数字与种子逐一对上（分层/五类/告警三口径/需求/配额）。"""
    seed_dataset(
        harness, name="销售明细", tier="api", row_count=120, size_bytes=4096,
        updated_at=at(0), key="sales_detail",
    )
    seed_dataset(
        harness, name="客户清单", tier="api", row_count=30, size_bytes=2048,
        updated_at=at(10), key="customer_list",
    )
    seed_dataset(
        harness, name="行为日志", tier="cell", row_count=9000, size_bytes=1048576,
        updated_at=at(20), key="behavior_log",
    )
    seed_subscription(harness, pack_id="macro-prices", created_at=at(5))
    seed_subscription(harness, pack_id="money-finance", created_at=at(15))

    board = seed_artifact(harness, kind="dashboard", status="published", title="经营看板", updated_at=at(30))
    seed_artifact(harness, kind="dashboard", status="draft", title="草稿看板", updated_at=at(31))
    rule_a = seed_artifact(harness, kind="alert_rule", status="published", title="价格告警", updated_at=at(32))
    rule_b = seed_artifact(harness, kind="alert_rule", status="draft", title="备用规则", updated_at=at(33))
    seed_artifact(harness, kind="composite_indicator", status="draft", title="复合指标", updated_at=at(34))
    seed_artifact(harness, kind="form", status="published", title="登记表单", updated_at=at(10))
    seed_artifact(harness, kind="research_report", status="archived", title="旧报告", updated_at=at(35))

    seed_alert_state(harness, artifact_id=rule_a, customer_id=CUST_A, state="firing", evaluated_at=at(40))
    seed_alert_state(harness, artifact_id=rule_b, customer_id=CUST_A, state="ok", evaluated_at=at(41))
    now = utc_now()
    seed_alert_event(harness, artifact_id=rule_a, customer_id=CUST_A, created_at=now - timedelta(hours=1))
    seed_alert_event(harness, artifact_id=rule_a, customer_id=CUST_A, created_at=now - timedelta(hours=2), kind="resolved")
    seed_alert_event(harness, artifact_id=rule_b, customer_id=CUST_A, created_at=now - timedelta(hours=30))

    seed_demand(harness, code="DE-20261010-01", status="submitted", updated_at=at(50))
    seed_demand(harness, code="DE-20261011-02", status="in_progress", updated_at=at(51))
    seed_demand(harness, code="DE-20261012-03", status="delivered", updated_at=at(52))

    resp = harness.client.get(URL, headers=HEADERS_A)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # datasets：总数 3 / api 2 / cell 1；最近按 updated_at 倒序（行为日志→客户清单→销售明细）
    assert body["datasets"]["total"] == 3
    assert body["datasets"]["api_tier"] == 2
    assert body["datasets"]["cell_tier"] == 1
    assert [item["name"] for item in body["datasets"]["recent"]] == ["行为日志", "客户清单", "销售明细"]
    top = body["datasets"]["recent"][0]
    assert top["storage_tier"] == "cell"
    assert top["row_count"] == 9000
    assert top["size_bytes"] == 1048576
    assert top["updated_at"] == at(20).isoformat()

    # free_quota：免费档真实数字（used = api 层数据集数；上限来自 datasets 切片配置默认）
    assert body["free_quota"] == {
        "used_datasets": 2,
        "max_datasets": 3,
        "max_rows_per_dataset": 10000,
    }

    # packs：订阅 2；最近倒序（money-finance 先），名称取静态定义逐字对上
    assert body["packs"]["subscribed"] == 2
    assert [item["pack_id"] for item in body["packs"]["recent"]] == ["money-finance", "macro-prices"]
    first = body["packs"]["recent"][0]
    assert first["name_zh"] == get_pack("money-finance").name_zh
    assert first["name_en"] == get_pack("money-finance").name_en
    assert first["subscribed_at"] == at(15).isoformat()

    # artifacts：活跃 6（archived 不计；form 计入总数与 by_kind）；八键全给
    # （缺类零，record-view / doc 键为后续变更追加）；最近倒序 ≤5
    # （form 恰好最旧被截断在 recent 之外）
    assert body["artifacts"]["active_total"] == 6
    assert body["artifacts"]["by_kind"] == {
        "dashboard": 2, "form": 1, "composite_indicator": 1,
        "research_report": 0, "alert_rule": 2, "refresh_schedule": 0,
        "record-view": 0, "doc": 0,
    }
    assert [item["title"] for item in body["artifacts"]["recent"]] == [
        "复合指标", "备用规则", "价格告警", "草稿看板", "经营看板",
    ]
    assert body["artifacts"]["recent"][0]["id"] != board  # id 为整数主键
    assert all(isinstance(item["id"], int) for item in body["artifacts"]["recent"])

    # alerts：alert_rule 活跃产物 2；firing 1（ok 不计）；24h 事件 2（30h 前不计）
    assert body["alerts"] == {"alert_rules": 2, "firing": 1, "events_24h": 2}

    # demands：开放 2（submitted + in_progress；delivered 不计）；最新一单为 updated_at 最大者
    assert body["demands"]["open"] == 2
    assert body["demands"]["latest"] == {
        "code": "DE-20261012-03", "status": "delivered",
        "updated_at": at(52).isoformat(),
    }


# ------------------------------------------------------------------ ②排序与截断


def test_recent_ordering_and_truncation(harness) -> None:
    """三段最近列表：倒序 + ≤5 截断（第 6/7 条不出现）。"""
    for index in range(7):
        seed_dataset(
            harness, name=f"数据集{index}", tier="api", updated_at=at(index),
            key=f"ds_{index:02d}",
        )
    pack_ids = [pack.id for pack in sorted(PACKS, key=lambda p: p.id)][:6]
    for index, pack_id in enumerate(pack_ids):
        seed_subscription(harness, pack_id=pack_id, created_at=at(index))
    for index in range(6):
        seed_artifact(
            harness, kind="dashboard", status="draft",
            title=f"看板{index}", updated_at=at(index),
        )

    body = harness.client.get(URL, headers=HEADERS_A).json()

    # datasets：7 条 → 最近 5 条倒序（数据集6…数据集2）
    assert [item["name"] for item in body["datasets"]["recent"]] == [
        "数据集6", "数据集5", "数据集4", "数据集3", "数据集2",
    ]
    assert body["datasets"]["total"] == 7  # 计数不截断

    # packs：6 订阅 → 最近 5 条倒序
    assert body["packs"]["subscribed"] == 6
    assert [item["pack_id"] for item in body["packs"]["recent"]] == list(reversed(pack_ids))[:5]

    # artifacts：6 活跃 → 最近 5 条倒序
    assert body["artifacts"]["active_total"] == 6
    assert [item["title"] for item in body["artifacts"]["recent"]] == [
        "看板5", "看板4", "看板3", "看板2", "看板1",
    ]


# ------------------------------------------------------------------ ③免费/付费配额


def test_free_quota_free_real_paid_null(harness) -> None:
    """免费档 A：真实数字（used 按 api 层口径）；付费档 B：整段 null。"""
    seed_dataset(harness, name="免费集一", tier="api", updated_at=at(0), key="fq_1")
    seed_dataset(harness, name="免费集二", tier="api", updated_at=at(1), key="fq_2")
    seed_dataset(harness, name="cell集不计", tier="cell", updated_at=at(2), key="fq_3")

    free = harness.client.get(URL, headers=HEADERS_A).json()
    assert free["free_quota"] == {
        "used_datasets": 2,  # cell 层不计入免费口径
        "max_datasets": 3,
        "max_rows_per_dataset": 10000,
    }

    seed_dataset(harness, customer_id=CUST_B, name="付费集", tier="cell", updated_at=at(3), key="fq_b1")
    paid = harness.client.get(URL, headers=HEADERS_B).json()
    assert paid["free_quota"] is None
    assert paid["datasets"]["total"] == 1  # 其余段照常聚合


# ------------------------------------------------------------------ ④跨客户隔离


def test_cross_customer_isolation(harness) -> None:
    """B 的名称/id/code/订阅/告警在 A 的响应全文零出现，计数不含 B。"""
    # A 的少量种子（保证未过滤的查询必然把 B 顶出来、计数必然被 B 撑大）
    a_dataset = seed_dataset(harness, name="A数据集", tier="api", updated_at=at(0), key="iso_a1")
    seed_subscription(harness, pack_id="macro-prices", created_at=at(1))
    seed_artifact(harness, kind="dashboard", status="published", title="A看板", updated_at=at(2))
    seed_demand(harness, code="DE-20261012-01", status="in_progress", updated_at=at(3))

    # B 的专属种子： distinctive 名称/标题/编码 + 独有包 + 告警状态与事件
    b_dataset = seed_dataset(
        harness, customer_id=CUST_B, name="B专属数据集", tier="cell",
        row_count=777, updated_at=at(99), key="iso_b1",
    )
    seed_subscription(harness, customer_id=CUST_B, pack_id="yearbook-panorama", created_at=at(98))
    b_artifact = seed_artifact(
        harness, customer_id=CUST_B, kind="dashboard", status="published",
        title="B专属看板", updated_at=at(97),
    )
    b_rule = seed_artifact(
        harness, customer_id=CUST_B, kind="alert_rule", status="published",
        title="B专属规则", updated_at=at(96),
    )
    seed_alert_state(harness, artifact_id=b_rule, customer_id=CUST_B, state="firing", evaluated_at=at(95))
    seed_alert_event(harness, artifact_id=b_rule, customer_id=CUST_B, created_at=utc_now())
    seed_alert_event(harness, artifact_id=b_artifact, customer_id=CUST_B, created_at=utc_now())
    seed_demand(harness, code="DE-20990101-BB", customer_id=CUST_B, status="submitted", updated_at=at(94))

    resp = harness.client.get(URL, headers=HEADERS_A)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # 计数只含 A：数据集 1 / 订阅 1 / 活跃产物 1 / alert_rule 0 / firing 0 / 事件 0 / 开放 1
    assert body["datasets"]["total"] == 1
    assert body["datasets"]["cell_tier"] == 0
    assert body["packs"]["subscribed"] == 1
    assert body["artifacts"]["active_total"] == 1
    assert body["alerts"] == {"alert_rules": 0, "firing": 0, "events_24h": 0}
    assert body["demands"]["open"] == 1
    assert body["demands"]["latest"]["code"] == "DE-20261012-01"

    # 响应全文断言：B 专属标识零出现（名称/标题/编码/独有包名/行数/
    # B 的数据集 uuid；产物主键是小整数会自然出现在计数里，不作 needle——
    # 产物隔离由标题 needle + active_total 计数断言共同咬合）
    text = resp.text
    for needle in (
        "B专属数据集", "B专属看板", "B专属规则", "DE-20990101-BB",
        "yearbook-panorama", "年鉴全景", "Yearbook Panorama",
        b_dataset, "777",
    ):
        assert needle not in text, f"跨客户泄漏：{needle!r} 出现在响应中"

    # A 自己的数据照常呈现（隔离不误伤）
    assert body["datasets"]["recent"][0]["id"] == a_dataset
    assert body["artifacts"]["recent"][0]["title"] == "A看板"


# ------------------------------------------------------------------ ⑤未登录 401


def test_anonymous_request_401(harness) -> None:
    """无 Bearer ID token：401（鉴权依赖负责，不产生聚合内容）。"""
    resp = harness.client.get(URL)
    assert resp.status_code == 401
    assert "datasets" not in resp.text  # 无任何聚合段泄漏


# ------------------------------------------------------------------ ⑥全空客户


def test_empty_customer_all_zero_sections(harness) -> None:
    """零数据免费客户：各段零值/空列表不炸；配额如实 0/3/10000。"""
    resp = harness.client.get(URL, headers=HEADERS_C)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["datasets"] == {
        "total": 0, "api_tier": 0, "cell_tier": 0, "recent": [],
    }
    assert body["free_quota"] == {
        "used_datasets": 0, "max_datasets": 3, "max_rows_per_dataset": 10000,
    }
    assert body["packs"] == {"subscribed": 0, "recent": []}
    assert body["artifacts"]["active_total"] == 0
    assert body["artifacts"]["by_kind"] == {kind: 0 for kind in ALL_KINDS}
    assert body["artifacts"]["recent"] == []
    assert body["alerts"] == {"alert_rules": 0, "firing": 0, "events_24h": 0}
    assert body["demands"] == {"open": 0, "latest": None}
