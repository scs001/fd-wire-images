"""admin 派发台「转生成工单」端点（wire-workorder-console-action 任务 1.4）。

``POST /internal/admin/demands/{id}/workorder`` 与内部端点共用
``export_workorder_service``——本文件只测 admin 面的接线与口径：成功
响应形状、拒绝口径（409/422/404）、append-only 重出、鉴权 fail-closed；
映射/脱敏/幂等的 service 本体语义见 test_demands_workorder.py 专测。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import yaml
from sqlalchemy.orm import sessionmaker

from fd_wire_api.demands import orchestration
from fd_wire_api.demands.models import Demand

from test_admin_api import AUTH, admin_env, seed_console_customers, submit_demand


@pytest.fixture()
def slug_free(monkeypatch):
    """slug 探测钉 free：网络面隔离（三态探测语义另有专测）。"""
    monkeypatch.setattr(orchestration, "check_slug_free", lambda slug, **kw: "free")


def _at_in_progress(client, customer_id: str = "cust-001") -> dict:
    """造数到 in_progress：客户面提交（带合规门两入参）→ 派发台接单。"""
    seed_console_customers(customer_id)
    demand = submit_demand(client, customer_id, "光伏装机")
    resp = client.post(
        f"/internal/admin/demands/{demand['id']}/transition",
        json={"target_status": "in_progress", "by": "admin-console"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _export(client, demand_id: int, **body) -> object:
    return client.post(
        f"/internal/admin/demands/{demand_id}/workorder",
        json=body or None,
        headers=AUTH,
    )


def _internal_view(client, demand_id: int) -> dict:
    resp = client.get(f"/internal/admin/demands", headers=AUTH)
    return next(d for d in resp.json() if d["id"] == demand_id)


def _make_legacy_in_progress(env) -> int:
    """直插一张无 source_urls 的存量 in_progress 单（合规门前旧数据形态）。"""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    sess = sessionmaker(bind=env.engine)()
    try:
        demand = Demand(
            code="DE-20260101-01",
            customer_id="cust-001",
            title="存量单（无源地址）",
            intent="旧八态时代提交的需求",
            status="in_progress",
            status_history=[],
            pipeline_events=[],
            delivery_events=[],
            linked_indicators=[],
            created_at=now,
            updated_at=now,
        )
        sess.add(demand)
        sess.commit()
        return demand.id
    finally:
        sess.close()


# ---------------------------------------------------------------------------
# 成功路径


def test_export_success_shape_and_append_only(admin_env, slug_free):
    client = admin_env.client
    demand = _at_in_progress(client)

    resp = _export(client, demand["id"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 响应五键与内部端点完全一致（端点共用 service 的接线断言）
    assert set(body) == {
        "ticket_yaml",
        "ticket_path",
        "delivered",
        "queued_for_pull",
        "slug_check",
    }
    assert body["delivered"] is False  # 出单时刻未投递——诚实降级红线
    assert body["queued_for_pull"] is True
    assert body["slug_check"] == "free"

    ticket = yaml.safe_load(body["ticket_yaml"])
    assert ticket["kind"] == "generate"
    assert ticket["unit"] == "spiders/stats-gov-cn/"  # 首源 hostname 派生
    # 脱敏红线：全文无联系邮箱；notes 留需求单编码与客户代号
    assert "example.com" not in body["ticket_yaml"]
    assert demand["code"] in ticket["brief"]["notes"]

    view = _internal_view(client, demand["id"])
    assert view["status"] == "in_progress"  # 出单不迁移状态
    exports = [
        e for e in view["pipeline_events"] if e["type"] == "workorder_exported"
    ]
    assert len(exports) == 1
    assert exports[0]["ticket_path"] == body["ticket_path"]
    assert exports[0]["sha256"]

    # 同单重出允许：append-only 追加第二条导出，历史不改写
    resp2 = _export(client, demand["id"])
    assert resp2.status_code == 200, resp2.text
    view2 = _internal_view(client, demand["id"])
    exports2 = [
        e for e in view2["pipeline_events"] if e["type"] == "workorder_exported"
    ]
    assert len(exports2) == 2


def test_slug_unverified_passes_honestly(admin_env, monkeypatch):
    """探测通道不可达（unverified）→ 如实放行，口径与内部端点一致。"""
    monkeypatch.setattr(
        orchestration, "check_slug_free", lambda slug, **kw: "unverified"
    )
    client = admin_env.client
    demand = _at_in_progress(client)

    resp = _export(client, demand["id"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["slug_check"] == "unverified"
    assert resp.json()["queued_for_pull"] is True


# ---------------------------------------------------------------------------
# 拒绝口径（与内部端点共用 service 的如实回绝）


def test_non_in_progress_rejected_409(admin_env):
    client = admin_env.client
    seed_console_customers("cust-001")
    demand = submit_demand(client, "cust-001", "未接单")  # submitted

    resp = _export(client, demand["id"])
    assert resp.status_code == 409
    assert "in_progress" in resp.json()["detail"]

    # 无任何留档产生
    view = _internal_view(client, demand["id"])
    assert view["pipeline_events"] == []


def test_no_source_urls_422(admin_env, slug_free):
    client = admin_env.client
    seed_console_customers("cust-001")
    demand_id = _make_legacy_in_progress(admin_env)

    resp = _export(client, demand_id)
    assert resp.status_code == 422
    assert "source_urls" in resp.json()["detail"]


def test_slug_occupied_409_transfer_to_human(admin_env, monkeypatch):
    monkeypatch.setattr(
        orchestration, "check_slug_free", lambda slug, **kw: "occupied"
    )
    client = admin_env.client
    demand = _at_in_progress(client)

    resp = _export(client, demand["id"])
    assert resp.status_code == 409
    assert "转人工" in resp.json()["detail"]


def test_unknown_demand_404(admin_env, slug_free):
    resp = admin_env.client.post(
        "/internal/admin/demands/99999/workorder", headers=AUTH
    )
    assert resp.status_code == 404


def test_bad_slug_override_422(admin_env, slug_free):
    client = admin_env.client
    demand = _at_in_progress(client)

    resp = _export(client, demand["id"], slug="!!!")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 鉴权（admin 双闸 fail-closed，与端点族既有口径一致）


def test_auth_fail_closed_missing_or_wrong_token(admin_env):
    client = admin_env.client
    demand = _at_in_progress(client)
    url = f"/internal/admin/demands/{demand['id']}/workorder"

    assert client.post(url).status_code == 401  # 缺头
    assert client.post(url, headers={"X-Admin-Token": "wrong"}).status_code == 401
    # 鉴权拒绝不产生任何留档
    view = _internal_view(client, demand["id"])
    assert view["pipeline_events"] == []
