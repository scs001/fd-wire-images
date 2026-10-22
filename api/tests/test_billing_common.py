"""计费测试共享设施（非 conftest，避免与并行代理争 tests/conftest.py）。

FakeSub2Api 用 httpx.MockTransport 承载，并模拟服务端 Idempotency-Key
幂等缓存——「同批次重放只扣一次」的净效果直接在其余额上可断言。
"""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from fd_wire_api.billing import Base, Customer, ensure_metering_records_table, seed_plans
from fd_wire_api.billing.sub2api import Sub2ApiClient

BASE = "http://sub2api.test"


def make_engine():
    """内存库：billing_* 表 + 临时 metering_records 表（耦合契约模拟）。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as sess:
        seed_plans(sess)
        sess.commit()
    with engine.begin() as conn:
        ensure_metering_records_table(conn)
    return engine


def add_customer(session: Session, *, name: str, plan_id: int,
                 sub2api_user_id: str, status: str = "active") -> Customer:
    customer = Customer(
        name=name, plan_id=plan_id, sub2api_user_id=sub2api_user_id, status=status
    )
    session.add(customer)
    session.flush()
    return customer


def insert_metering(engine, *, customer_ref: str, ts: str, rows: int,
                    track: str = "customer", request_id: str | None = None) -> None:
    """往临时 metering_records 表塞一行（列结构与 metering 侧 alembic 0001 对齐）。"""
    stmt = (
        "INSERT INTO metering_records "
        "(event_timestamp, request_id, caller, credential_track, server_name, "
        " rows_returned, source) "
        "VALUES (:ts, :rid, :caller, :track, 'business-mcp', :rows, 'customer')"
    )
    params = {
        "ts": ts,
        "rid": request_id or f"req-{customer_ref}-{ts}-{rows}-{track}",
        "caller": customer_ref,
        "track": track,
        "rows": rows,
    }
    with engine.begin() as c:
        c.execute(text(stmt), params)


class FakeSub2Api:
    """sub2api admin 面的内存假件（2026-10-09 真契约形态）：
    x-api-key 鉴权、`/api/v1/admin/*` 路径、envelope {code:0,message,data}、
    email 唯一建户 + balance{balance,operation} 幂等更新。"""

    def __init__(self) -> None:
        self.users: dict[str, dict] = {}
        self.calls: list[dict] = []
        self.idem_cache: dict[str, dict] = {}
        self.fail_adjust_left = 0   # 接下来 N 次 adjust 返回 503
        self.fail_reads_left = 0    # 接下来 N 次 GET 返回 503

    # -- 数据准备 -----------------------------------------------------------

    def add_user(self, user_id: str, *, name: str = "user",
                 balance_micro: int = 0, email: str | None = None) -> str:
        self.users[user_id] = {
            "name": name, "balance_micro": balance_micro,
            "email": email or f"{user_id}@fake.internal", "status": "active",
        }
        return user_id

    @property
    def adjust_calls(self) -> list[dict]:
        return [
            c for c in self.calls
            if c["op"] == "POST" and c["url"].endswith("/balance")
        ]

    def balance(self, user_id: str) -> int:
        return self.users[user_id]["balance_micro"]

    # -- HTTP 面 ------------------------------------------------------------

    @staticmethod
    def _envelope(data) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "message": "success", "data": data})

    def handler(self, request: httpx.Request) -> httpx.Response:
        op, url = request.method, str(request.url)
        idem = request.headers.get("Idempotency-Key")
        self.calls.append({"op": op, "url": url, "idem": idem,
                           "body": request.content.decode() if request.content else None})
        api = "/api/v1/admin/users"
        if op == "GET" and f"{api}/" in url:
            if self.fail_reads_left > 0:
                self.fail_reads_left -= 1
                return httpx.Response(503, text="billing plane down")
            user_id = url.rstrip("/").rsplit("/", 1)[-1]
            user = self.users.get(user_id)
            if user is None:
                return httpx.Response(404, json={"code": 1, "message": "no such user"})
            return self._envelope({"id": user_id, **user})
        if op == "GET" and url.split("?")[0].endswith(api):
            search = request.url.params.get("search", "")
            items = [
                {"id": uid, **u} for uid, u in self.users.items()
                if search.lower() in str(u.get("email", "")).lower()
            ]
            return self._envelope({"items": items})
        if op == "POST" and url.endswith(api):
            body = json.loads(request.content or b"{}")
            email = str(body.get("email") or "")
            if any(u["email"].lower() == email.lower() for u in self.users.values()):
                return httpx.Response(200, json={"code": 1, "message": "email already exists"})
            digest = hashlib.sha1(email.encode()).hexdigest()[:8]
            user_id = f"su-{digest}"
            self.users[user_id] = {
                "name": body.get("username", ""), "balance_micro": int(body.get("balance") or 0),
                "email": email, "status": "active",
            }
            return self._envelope({"id": user_id, **self.users[user_id]})
        if op == "POST" and "/balance" in url:
            if self.fail_adjust_left > 0:
                self.fail_adjust_left -= 1
                return httpx.Response(503, text="adjust boom")
            user_id = url.rstrip("/").rsplit("/users/", 1)[-1].split("/")[0]
            if idem and idem in self.idem_cache:
                return self._envelope(self.idem_cache[idem])
            user = self.users.get(user_id)
            if user is None:
                return httpx.Response(404, json={"code": 1, "message": "no such user"})
            body = json.loads(request.content or b"{}")
            amount = int(body["balance"])
            operation = body.get("operation", "set")
            user["balance_micro"] = (
                amount if operation == "set"
                else user["balance_micro"] + amount if operation == "add"
                else user["balance_micro"] - amount
            )
            payload = {"id": user_id, **user}
            if idem:
                self.idem_cache[idem] = payload
            return self._envelope(payload)
        return httpx.Response(404, json={"code": 1, "message": f"unmatched {op} {url}"})

    def client(self) -> Sub2ApiClient:
        return Sub2ApiClient(
            base_url=BASE,
            admin_key="test-admin-key",
            http_client=httpx.Client(transport=httpx.MockTransport(self.handler)),
        )


@pytest.fixture()
def engine():
    return make_engine()


@pytest.fixture()
def fake_sub2api() -> FakeSub2Api:
    return FakeSub2Api()
