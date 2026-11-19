"""冒烟测试：/health 200 与示例路由形状。"""

from fastapi.testclient import TestClient

from fd_wire_api.app import app


def test_health_ok() -> None:
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_contracts_shape() -> None:
    client = TestClient(app)
    resp = client.get("/api/v1/contracts")
    assert resp.status_code == 200
    contracts = resp.json()["contracts"]
    assert set(contracts) == {
        "business_mcp_url",
        "registry_url",
        "sub2api_base",
        "token_gateway_url",
    }
