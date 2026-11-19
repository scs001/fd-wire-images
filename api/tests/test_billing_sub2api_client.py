"""sub2api admin 客户端：全 mock 覆盖（真契约：x-api-key + /api/v1/admin + envelope）。

2026-10-09 随跨线挂账销账重写——v1 约定（/admin/api/users + Bearer +
amount_micro）在真实面不存在，见 billing/sub2api.py 模块文档与
lex-platform-v1 5.3 演练报告。
"""

from __future__ import annotations

import json

import httpx
import pytest

from fd_wire_api.billing.sub2api import (
    MIN_PASSWORD_LENGTH,
    Sub2ApiClient,
    Sub2ApiError,
    Sub2ApiUnavailable,
    parse_user,
)

from test_billing_common import FakeSub2Api


def make_client(fake: FakeSub2Api) -> Sub2ApiClient:
    return fake.client()


def test_create_user_posts_email_password_with_x_api_key() -> None:
    fake = FakeSub2Api()
    with make_client(fake) as client:
        user = client.create_user(
            email="cust@wire.internal", password="secret-pw", username="cust",
            idempotency_key="cust-1",
        )
    assert user.id.startswith("su-") and user.email == "cust@wire.internal"
    call = fake.calls[0]
    assert call["op"] == "POST" and call["url"].endswith("/api/v1/admin/users")
    body = json.loads(call["body"])
    assert body["email"] == "cust@wire.internal" and body["password"] == "secret-pw"


def test_create_user_rejects_short_password() -> None:
    fake = FakeSub2Api()
    with make_client(fake) as client:
        with pytest.raises(ValueError):
            client.create_user(email="a@b.c", password="x" * (MIN_PASSWORD_LENGTH - 1))


def test_duplicate_email_is_business_error() -> None:
    fake = FakeSub2Api()
    with make_client(fake) as client:
        client.create_user(email="dup@wire.internal", password="secret-pw")
        with pytest.raises(Sub2ApiError, match="already exists"):
            client.create_user(email="dup@wire.internal", password="secret-pw")


def test_find_user_by_email_hits_exactly() -> None:
    fake = FakeSub2Api()
    fake.add_user("u1", email="hit@wire.internal")
    fake.add_user("u2", email="other@wire.internal")
    with make_client(fake) as client:
        hit = client.find_user_by_email("HIT@wire.internal")  # 大小写不敏感
        miss = client.find_user_by_email("ghost@wire.internal")
    assert hit is not None and hit.id == "u1"
    assert miss is None


def test_read_user_parses_flat_and_wrapped_payloads() -> None:
    assert parse_user({"id": "u1", "name": "n", "balance_micro": -390_000_000}).balance_micro == -390_000_000
    # 实测契约：数值单位与写入一致、无换算（float 字面按微元取整；round=银行家舍入）
    assert parse_user({"user": {"id": "u1", "name": "n", "balance": "12.5"}}).balance_micro == 12
    assert parse_user({"user": {"id": 7, "balance_micro": 0}}).id == "7"
    assert parse_user({"id": "u8", "balance": 3, "email": "e@x", "status": "active"}).email == "e@x"


def test_adjust_balance_applies_once_per_idempotency_key() -> None:
    fake = FakeSub2Api()
    fake.add_user("u1", balance_micro=1_000_000)
    with make_client(fake) as client:
        after1 = client.adjust_balance(user_id="u1", amount_micro=-400_000,
                                       idempotency_key="settle-1")
        after2 = client.adjust_balance(user_id="u1", amount_micro=-400_000,
                                       idempotency_key="settle-1")
    assert after1.balance_micro == after2.balance_micro == 600_000  # 只扣一次
    assert fake.balance("u1") == 600_000
    adjust_calls = [c for c in fake.calls if c["url"].endswith("/balance")]
    assert all(c["idem"] == "settle-1" for c in adjust_calls)
    # 符号折进 operation、数值发绝对值（契约 balance>0）
    assert json.loads(adjust_calls[0]["body"]) == {"balance": 400_000, "operation": "subtract"}


def test_adjust_balance_requires_idempotency_key_and_nonzero() -> None:
    fake = FakeSub2Api()
    fake.add_user("u1")
    with make_client(fake) as client:
        with pytest.raises(ValueError):
            client.adjust_balance(user_id="u1", amount_micro=-1, idempotency_key="")
        with pytest.raises(ValueError):
            client.adjust_balance(user_id="u1", amount_micro=0, idempotency_key="k")


def test_probe_sufficient_and_insufficient() -> None:
    fake = FakeSub2Api()
    fake.add_user("u1", balance_micro=100)
    with make_client(fake) as client:
        ok = client.probe("u1", min_balance_micro=50)
        low = client.probe("u1", min_balance_micro=200)
    assert ok.reachable and ok.sufficient
    assert low.reachable and not low.sufficient


def test_probe_fail_closed_on_unreachable_and_missing_user() -> None:
    fake = FakeSub2Api()
    fake.fail_reads_left = 5  # 503 → 不可用
    with make_client(fake) as client:
        down = client.probe("u1")
    assert not down.reachable and not down.sufficient and down.error

    with make_client(FakeSub2Api()) as client:  # 404 → fail-closed
        missing = client.probe("ghost")
    assert not missing.reachable and not missing.sufficient


def test_probe_fail_closed_on_transport_error() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = Sub2ApiClient(
        base_url="http://sub2api.test",
        admin_key="k",
        http_client=httpx.Client(transport=httpx.MockTransport(broken)),
    )
    result = client.probe("u1")
    assert not result.reachable and not result.sufficient


def test_error_surfaces() -> None:
    fake = FakeSub2Api()
    fake.fail_adjust_left = 2
    fake.add_user("u1")
    with make_client(fake) as client:
        with pytest.raises(Sub2ApiUnavailable):  # 5xx → 计费面不可用语义
            client.adjust_balance(user_id="u1", amount_micro=-1, idempotency_key="k")
        with pytest.raises(Sub2ApiError) as missing:  # 4xx → 业务错误
            client.read_user("ghost")
        assert missing.value.status_code == 404


def test_bad_payload_surfaces() -> None:
    with pytest.raises(Sub2ApiError):
        parse_user({"name": "no-id"})
    with pytest.raises(Sub2ApiError):
        parse_user({"id": "u", "balance": "not-a-number"})


def test_non_json_response_raises() -> None:
    client = Sub2ApiClient(
        base_url="http://sub2api.test",
        admin_key="k",
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>"))
        ),
    )
    with pytest.raises(Sub2ApiError):
        client.read_user("u1")


def test_ensure_customer_end_to_end_with_mock_http() -> None:
    from sqlalchemy.orm import Session

    from fd_wire_api.billing import ensure_customer
    from test_billing_common import make_engine

    engine = make_engine()
    fake = FakeSub2Api()
    with Session(engine) as session, make_client(fake) as client:
        customer, created = ensure_customer(
            session, client, name="萬星数据", plan_code="professional",
            idempotency_key="cust-wx",
        )
        session.commit()
        assert created and customer.status == "active"
        assert customer.sub2api_user_id in fake.users
        assert customer.plan.code == "professional"

        again, created2 = ensure_customer(
            session, client, name="萬星数据", plan_code="professional",
            idempotency_key="cust-wx",
        )
        session.commit()
        assert not created2 and again.id == customer.id
        assert len(fake.users) == 1  # 重试未重复建支付账户（email 前置消解）

        with pytest.raises(ValueError):
            ensure_customer(session, client, name="X", plan_code="platinum")
