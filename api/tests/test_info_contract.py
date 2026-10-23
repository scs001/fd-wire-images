from fd_wire_api.artifacts.info_contract import (
    InfoUnavailable,
    info_refresh_plan,
    parse_info_query,
    refresh_spec_with_info,
)


class FakeClient:
    def __init__(self, items=None, fail=None):
        self.items = items or []
        self.fail = fail
        self.queries = []

    def list_items(self, query):
        self.queries.append(query)
        if self.fail:
            raise InfoUnavailable(self.fail)
        return {"items": self.items}


def panel(id="p1", **info):
    return {
        "id": id,
        "type": "list",
        "title": "信息流",
        "span": 3,
        "encoding": {"title": "标题", "summary": "摘要", "time": "时间", "source": "来源", "link": "链接"},
        "snapshot": {"columns": ["old"], "rows": [["旧快照"]]},
        "info_query": info or {"sources": ["cninfo"], "limit": 10},
    }


def spec(*panels):
    return {"title": "看板", "spec": {"title": "看板", "panels": list(panels)}}


def item(n=1):
    return {
        "title": f"公告{n}",
        "summary": "摘要",
        "published_at": "2026-10-20T09:00:00Z",
        "source": "cninfo",
        "url": "https://example/1",
    }


def test_parse_info_query_drops_invalid_and_clamps():
    assert parse_info_query({"limit": 9999})["limit"] == 200
    assert parse_info_query({"limit": 0})["limit"] == 1
    assert parse_info_query({"limit": "x"})["limit"] == 20
    assert parse_info_query({"published_after": "2026/10/20"}).get("published_after") is None
    assert parse_info_query("nope") is None


def test_plan_only_list_panels_with_query():
    line = panel("p1")
    no_query = {**panel("p2"), "info_query": None}
    chart = {"id": "p3", "type": "line", "info_query": {"limit": 5}}
    plan = info_refresh_plan(spec(line, no_query, chart))
    assert [p[1]["id"] for p in plan] == ["p1"]


def test_refresh_maps_roles_and_clears_lag():
    p = panel("p1", sources=["cninfo"], limit=10)
    p["info_lag"] = {"at": "old", "reason": "old"}
    new, results = refresh_spec_with_info(spec(p), FakeClient([item()]), now="2026-10-23T00:00:00Z")
    out = new["spec"]["panels"][0]
    assert results[0].status == "ok" and results[0].count == 1
    assert out["snapshot"]["columns"] == ["标题", "摘要", "时间", "来源", "链接"]
    assert out["snapshot"]["rows"] == [["公告1", "摘要", "2026-10-20T09:00:00Z", "cninfo", "https://example/1"]]
    assert "info_lag" not in out


def test_missing_client_keeps_snapshot_and_marks_lag():
    new, results = refresh_spec_with_info(spec(panel()), None, now="2026-10-23T00:00:00Z")
    out = new["spec"]["panels"][0]
    assert results[0].status == "lag"
    assert out["snapshot"]["rows"] == [["旧快照"]]
    assert "fd-info 未配置" in out["info_lag"]["reason"]


def test_unavailable_keeps_snapshot_and_marks_lag():
    new, results = refresh_spec_with_info(
        spec(panel()), FakeClient(fail="连接失败"), now="2026-10-23T00:00:00Z"
    )
    out = new["spec"]["panels"][0]
    assert results[0].status == "lag"
    assert out["snapshot"]["rows"] == [["旧快照"]]
    assert out["info_lag"]["reason"] == "连接失败"


def test_one_panel_failure_does_not_affect_another():
    class SplitClient:
        def list_items(self, query):
            if query.get("sources") == ["bad"]:
                raise InfoUnavailable("bad source")
            return {"items": [item()]}

    good = panel("good", sources=["cninfo"])
    bad = panel("bad", sources=["bad"])
    new, results = refresh_spec_with_info(spec(good, bad), SplitClient(), now="2026-10-23T00:00:00Z")
    by_id = {r.panel_id: r for r in results}
    assert by_id["good"].status == "ok"
    assert by_id["bad"].status == "lag"
    panels = {p["id"]: p for p in new["spec"]["panels"]}
    assert panels["good"]["snapshot"]["rows"][0][0] == "公告1"
    assert panels["bad"]["snapshot"]["rows"] == [["旧快照"]]
