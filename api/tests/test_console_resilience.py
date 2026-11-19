"""档位解析确定性与同名守卫（wire-console-resilience 任务组 1/2）。

覆盖：
- 1.2 解析确定性（design D1）：同名「真户（sub 非空）+ 孤儿（sub 空）」
  → 收敛真户；双孤儿 → id 最小；单行零漂移；无行 None；不抛
  MultipleResultsFound（平台库不可达回落路径回归）；
- 2.4 建档守卫（design D3）：``ensure_customer`` 同名幂等（同档 + 跨档
  复用既有行、不重复建 sub2api 账户；IntegrityError 兜底分支同键）；
- 2.4 迁移自愈（design D2）：0037 重名组按 D1 规则改名（``#orphan-<id>``
  后缀）、唯一索引建立、``#orphan-`` 已改名形态幂等容忍、downgrade 只删
  索引不改名 + 往返重跑。

环境纪律：解析确定性用例把平台库钉在 **0036 版本位**（唯一索引建立前的
真实缺陷形态——重名行可落库）；建档守卫用内存库 + FakeSub2Api
（test_billing_sub2api_client 同款）；迁移用临时文件库真跑 alembic
（test_datasets_migrations 同款）。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import false, select
from sqlalchemy.orm import Session
from sqlalchemy import create_engine, inspect

from fd_wire_api.app import app  # noqa: F401 - 预热：解 datasets↔ai/entities 导入环（仓内既有导入序依赖）
from fd_wire_api.datasets import service
from test_billing_common import FakeSub2Api, make_engine

API_DIR = Path(__file__).resolve().parent.parent


def _alembic_config() -> Config:
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "alembic"))
    return cfg


# ---------------------------------------------------------------------------
# 1.2 档位解析确定性（design D1）——平台库钉在 0036（唯一索引前形态）
# ---------------------------------------------------------------------------


@pytest.fixture()
def plan_db(tmp_path, monkeypatch):
    """0036 版本位的平台库（``billing_customers.name`` 无唯一约束=修复前
    的真实形态）：重名行可落库，供 D1 解析用例；含 seed 档位。"""
    from fd_wire_api.admin import reset_admin_runtime
    from fd_wire_api.billing.models import Base as BillingBase
    from fd_wire_api.billing.plans import seed_plans

    db_url = f"sqlite:///{tmp_path/'plan.db'}"
    monkeypatch.setenv("FD_WIRE_DATABASE_URL", db_url)
    monkeypatch.setenv("FD_WIRE_ADMIN_DATABASE_URL", db_url)
    reset_admin_runtime()

    command.upgrade(_alembic_config(), "0036_artifact_marketplace")

    engine = create_engine(db_url)
    BillingBase.metadata.create_all(engine)  # metering_records 临时表等
    with Session(engine) as sess:
        plans = seed_plans(sess)
        sess.commit()
        plan_ids = {code: p.id for code, p in plans.items()}
    engine.dispose()

    yield SimpleNamespace(db_url=db_url, plan_ids=plan_ids)
    reset_admin_runtime()


def _plan_engine(db_url: str):
    from fd_wire_api.billing.models import Base as BillingBase

    engine = create_engine(db_url)
    BillingBase.metadata.create_all(engine)
    return engine


def _add_customer(db_url: str, *, name: str, plan_id: int,
                  logto_sub: str | None, sub2api_user_id: str | None = None) -> int:
    """裸落一行客户（0036 形态下重名合法），返回 id。"""
    from fd_wire_api.billing.models import Customer

    engine = _plan_engine(db_url)
    try:
        with Session(engine) as sess:
            row = Customer(
                name=name, plan_id=plan_id, logto_sub=logto_sub,
                sub2api_user_id=sub2api_user_id,
            )
            sess.add(row)
            sess.commit()
            return row.id
    finally:
        engine.dispose()


def test_orphan_row_no_longer_hijacks_plan(plan_db) -> None:
    """孤儿行不再劫持档位（spec 场景 1）：同名「真户 free（sub 非空）+
    孤儿 standard（sub 空）」→ plan 解析收敛真户（free），不抛
    MultipleResultsFound 也不返回 None。"""
    # 先造孤儿（id 更小、standard 档带 storage_bytes），真户后插（id 大、sub 非空）
    _add_customer(
        plan_db.db_url, name="cust-001", plan_id=plan_db.plan_ids["standard"],
        logto_sub=None, sub2api_user_id=None,
    )
    _add_customer(
        plan_db.db_url, name="cust-001", plan_id=plan_db.plan_ids["free"],
        logto_sub="sub-cust-a", sub2api_user_id="caller-1",
    )

    assert service.plan_code_for(customer_id="cust-001") == "free"
    # storage_limit_for 同一助手：孤儿行（standard，5GiB）不再劫持——
    # 真户 free 档无 storage_bytes → 回落全局缺省
    assert service.storage_limit_for(customer_id="cust-001") == (
        service.get_settings().dataset_storage_limit_bytes
    )
    assert service.is_free_tier(customer_id="cust-001") is True


def test_double_orphans_converge_to_min_id(plan_db) -> None:
    """双孤儿同名（全无 sub）→ id 最小一行（确定性，不抛多行异常）。
    spec「全无 sub 才 fail-closed」的行级形态：id 最小行挂什么档就解析
    什么档（fail-closed 方向不因解析规则改变）。"""
    _add_customer(
        plan_db.db_url, name="cust-001", plan_id=plan_db.plan_ids["free"],
        logto_sub=None, sub2api_user_id=None,
    )
    _add_customer(
        plan_db.db_url, name="cust-001", plan_id=plan_db.plan_ids["standard"],
        logto_sub=None, sub2api_user_id=None,
    )

    assert service.plan_code_for(customer_id="cust-001") == "free"


def test_single_row_zero_drift(plan_db) -> None:
    """单行零漂移：同名守卫引入前后的既有语义逐字一致。"""
    from fd_wire_api.billing.models import Customer

    engine = _plan_engine(plan_db.db_url)
    try:
        with Session(engine) as sess:
            rows = sess.execute(
                select(Customer).where(Customer.name == "solo")
            ).scalars().all()
            assert rows == []  # 前置：无行
            sess.add(Customer(
                name="solo", plan_id=plan_db.plan_ids["basic"],
                logto_sub="sub-solo", sub2api_user_id="caller-solo",
            ))
            sess.commit()
    finally:
        engine.dispose()

    assert service.plan_code_for(customer_id="solo") == "basic"
    assert service.is_free_tier(customer_id="solo") is False
    assert service.storage_tier_for(customer_id="solo") == service.TIER_CELL
    # basic 无 storage_bytes → 全局缺省（既有回落语义）
    assert service.storage_limit_for(customer_id="solo") == (
        service.get_settings().dataset_storage_limit_bytes
    )


def test_unknown_customer_returns_none(plan_db) -> None:
    """无行 → None（fail-closed 原语义不变，不放大权限）。"""
    assert service.plan_code_for(customer_id="ghost") is None


def test_plan_code_for_never_raises_multiple_results(plan_db, monkeypatch) -> None:
    """守卫回归（spec：MUST NOT 因多行抛异常）：三行重名（两孤儿 + 真户）
    下解析照常收敛真户；平台库炸掉仍走既有回落（None / 全局缺省）。"""
    _add_customer(
        plan_db.db_url, name="cust-002", plan_id=plan_db.plan_ids["free"],
        logto_sub=None, sub2api_user_id=None,
    )
    _add_customer(
        plan_db.db_url, name="cust-002", plan_id=plan_db.plan_ids["standard"],
        logto_sub=None, sub2api_user_id=None,
    )
    _add_customer(
        plan_db.db_url, name="cust-002", plan_id=plan_db.plan_ids["basic"],
        logto_sub="sub-cust-b", sub2api_user_id="caller-2",
    )

    assert service.plan_code_for(customer_id="cust-002") == "basic"
    assert service.is_free_tier(customer_id="cust-002") is False

    # 平台库不可达：异常路径回落不变
    import fd_wire_api.admin.db as admin_db

    def _boom():
        raise RuntimeError("platform db down")

    monkeypatch.setattr(admin_db, "get_session", _boom)
    assert service.plan_code_for(customer_id="cust-002") is None
    assert service.storage_limit_for(customer_id="cust-002") == (
        service.get_settings().dataset_storage_limit_bytes
    )


# ---------------------------------------------------------------------------
# 2.4 建档守卫（design D3）：ensure_customer 单 name 幂等
# ---------------------------------------------------------------------------


def _customer_by_name(session: Session, name: str):
    from fd_wire_api.billing.models import Customer

    return session.execute(
        select(Customer).where(Customer.name == name)
    ).scalar_one()


def test_ensure_customer_same_plan_idempotent() -> None:
    """同档同名幂等（既有语义回归）：第二调用返回既有行、created=False、
    不重复建 sub2api 账户。"""
    from fd_wire_api.billing import ensure_customer

    engine = make_engine()
    fake = FakeSub2Api()
    with Session(engine) as session, fake.client() as client:
        first, created = ensure_customer(
            session, client, name="客户甲", plan_code="free",
            idempotency_key="k1",
        )
        session.commit()
        assert created is True

        again, created2 = ensure_customer(
            session, client, name="客户甲", plan_code="free",
            idempotency_key="k2",
        )
        session.commit()
        assert created2 is False and again.id == first.id
        assert len(fake.users) == 1


def test_ensure_customer_cross_plan_same_name_reuses_row() -> None:
    """跨档同名幂等（spec「自开户撞同名幂等」）：库中已有同名（free）行，
    另一档（standard）的 ensure_customer 调用幂等返回既有行——不造第二行；
    随后 sub 回写落在既有行上（onboarding 终态步骤的落点）。"""
    from fd_wire_api.billing import ensure_customer

    engine = make_engine()
    fake = FakeSub2Api()
    with Session(engine) as session, fake.client() as client:
        first, created = ensure_customer(
            session, client, name="撞名客户", plan_code="free",
            idempotency_key="k-free",
        )
        session.commit()
        assert created is True

        reused, created2 = ensure_customer(
            session, client, name="撞名客户", plan_code="standard",
            idempotency_key="k-std",
        )
        session.commit()
        assert created2 is False and reused.id == first.id
        assert _customer_by_name(session, "撞名客户").id == first.id

        # sub 回写（onboarding 终态）落既有行
        reused.logto_sub = "sub-wangming"
        session.commit()
        row = _customer_by_name(session, "撞名客户")
        assert row.id == first.id and row.logto_sub == "sub-wangming"


def test_ensure_customer_integrity_fallback_by_name(monkeypatch) -> None:
    """IntegrityError 兜底分支同改：唯一索引冲突后，兜底查重按单 name
    命中（跨档也幂等返回），不抛 IntegrityError。

    竞态形态复现：首查（返回 None，模拟「首查后并发行才提交」的窗口）→
    sub2api 代建 → 插入撞 name 唯一约束 → 兜底按 name 命中既有行。"""
    from fd_wire_api.billing import ensure_customer
    from fd_wire_api.billing import customers as customers_mod
    from fd_wire_api.billing.models import Customer, Plan

    engine = make_engine()
    fake = FakeSub2Api()
    with Session(engine) as session, fake.client() as client:
        real_existing = Customer(
            name="竞态同名",
            plan_id=session.execute(
                select(Plan).where(Plan.code == "basic")
            ).scalar_one().id,
            sub2api_user_id="caller-race",
        )
        session.add(real_existing)
        session.commit()

        # 竞态窗注入：仅第一次 select（ensure_customer 首查）恒空
        real_select = customers_mod.select
        first_check = {"done": False}

        def racing_select(*args, **kwargs):
            stmt = real_select(*args, **kwargs)
            if not first_check["done"]:
                first_check["done"] = True
                return stmt.where(false())
            return stmt

        monkeypatch.setattr(customers_mod, "select", racing_select)
        customer, created = ensure_customer(
            session, client, name="竞态同名", plan_code="free",
            idempotency_key="k-race",
        )
        session.commit()
        fallback_id = customer.id

    assert created is False
    assert fallback_id == real_existing.id
    # 库内仍只有一行（唯一索引前守卫的兜底语义：不新增）
    with Session(engine) as session:
        names = session.execute(select(Customer.name)).scalars().all()
        assert names == ["竞态同名"]


# ---------------------------------------------------------------------------
# 2.4 迁移自愈（design D2）：0037 重名改名 + 唯一索引
# ---------------------------------------------------------------------------


def _unique_name_indexes(db_url: str) -> set[str]:
    engine = create_engine(db_url)
    try:
        return {
            ix["name"]
            for ix in inspect(engine).get_indexes("billing_customers")
            if ix["unique"]
        }
    finally:
        engine.dispose()


def _seed_plans_in(db_url: str) -> dict[str, int]:
    from fd_wire_api.billing.models import Base as BillingBase
    from fd_wire_api.billing.plans import seed_plans

    engine = create_engine(db_url)
    BillingBase.metadata.create_all(engine)
    with Session(engine) as sess:
        plans = seed_plans(sess)
        sess.commit()
        ids = {code: p.id for code, p in plans.items()}
    engine.dispose()
    return ids


def _raw_names(db_url: str) -> list[tuple[str, str | None]]:
    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            return [
                (name, sub)
                for name, sub in conn.exec_driver_sql(
                    "SELECT name, logto_sub FROM billing_customers ORDER BY id"
                ).fetchall()
            ]
    finally:
        engine.dispose()


def test_migration_heals_duplicates_and_builds_unique_index(tmp_path, monkeypatch) -> None:
    """迁移自愈（spec 场景「迁移自愈」）：同名多行（孤儿 + 真户）→ 孤儿
    改名 ``#orphan-<id>``、真户原名不变；唯一索引建立成功。"""
    db_url = f"sqlite:///{tmp_path/'heal.db'}"
    monkeypatch.setenv("FD_WIRE_DATABASE_URL", db_url)

    # 在 0036 版本位先落库种重名（唯一索引只在 0037 建立后生效）
    command.upgrade(_alembic_config(), "0036_artifact_marketplace")
    plan_ids = _seed_plans_in(db_url)

    engine = create_engine(db_url)
    with engine.begin() as conn:
        # 真户（sub 非空，id=1）、孤儿（sub 空，id=2）同名
        conn.exec_driver_sql(
            "INSERT INTO billing_customers (name, plan_id, status, created_at)"
            " VALUES ('wang@x.com', :free, 'active', CURRENT_TIMESTAMP)",
            {"free": plan_ids["free"]},
        )
        conn.exec_driver_sql(
            "UPDATE billing_customers SET logto_sub = 'sub-wang' WHERE id = 1"
        )
        conn.exec_driver_sql(
            "INSERT INTO billing_customers (name, plan_id, status, created_at)"
            " VALUES ('wang@x.com', :std, 'active', CURRENT_TIMESTAMP)",
            {"std": plan_ids["standard"]},
        )
    engine.dispose()

    command.upgrade(_alembic_config(), "head")

    names = _raw_names(db_url)
    by_name: dict[str, list[str | None]] = {}
    for name, sub in names:
        by_name.setdefault(name, []).append(sub)
    # 真户原名保留；孤儿行获得 #orphan-<id> 后缀（生产止血同款）
    assert by_name["wang@x.com"] == ["sub-wang"]
    orphan_names = [n for n in by_name if n != "wang@x.com"]
    assert len(orphan_names) == 1
    assert orphan_names[0].startswith("wang@x.com#orphan-")
    assert "uq_billing_customers_name" in _unique_name_indexes(db_url)


def test_migration_tolerates_pre_renamed_orphan_form(tmp_path, monkeypatch) -> None:
    """生产已手工止血的 ``#orphan-std`` 形态：无重名组 → 零改名、只建
    索引（迁移幂等容忍，不二次改写）。"""
    db_url = f"sqlite:///{tmp_path/'pre_renamed.db'}"
    monkeypatch.setenv("FD_WIRE_DATABASE_URL", db_url)

    command.upgrade(_alembic_config(), "0036_artifact_marketplace")
    plan_ids = _seed_plans_in(db_url)
    engine = create_engine(db_url)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO billing_customers (name, plan_id, status, created_at)"
            " VALUES ('platform-test@findmail.com', :std, 'active', CURRENT_TIMESTAMP)",
            {"std": plan_ids["standard"]},
        )
        conn.exec_driver_sql(
            "UPDATE billing_customers SET logto_sub = 'sub-pt' WHERE id = 1"
        )
        conn.exec_driver_sql(
            "INSERT INTO billing_customers (name, plan_id, status, created_at)"
            " VALUES ('platform-test@findmail.com#orphan-std', :free, 'active',"
            " CURRENT_TIMESTAMP)",
            {"free": plan_ids["free"]},
        )
    engine.dispose()

    command.upgrade(_alembic_config(), "head")

    names = [n for n, _ in _raw_names(db_url)]
    assert names == [
        "platform-test@findmail.com", "platform-test@findmail.com#orphan-std",
    ]
    assert "uq_billing_customers_name" in _unique_name_indexes(db_url)


def test_migration_all_orphan_group_keeps_min_id_original(tmp_path, monkeypatch) -> None:
    """全孤儿重名组：保留 id 最小一行原名（与解析助手收敛方向一致）。"""
    db_url = f"sqlite:///{tmp_path/'all_orphan.db'}"
    monkeypatch.setenv("FD_WIRE_DATABASE_URL", db_url)

    command.upgrade(_alembic_config(), "0036_artifact_marketplace")
    plan_ids = _seed_plans_in(db_url)
    engine = create_engine(db_url)
    with engine.begin() as conn:
        for _ in range(2):
            conn.exec_driver_sql(
                "INSERT INTO billing_customers (name, plan_id, status, created_at)"
                " VALUES ('orphans@x.com', :pid, 'active', CURRENT_TIMESTAMP)",
                {"pid": plan_ids["free"]},
            )
    engine.dispose()

    command.upgrade(_alembic_config(), "head")
    names = sorted(n for n, _ in _raw_names(db_url))
    assert names[0] == "orphans@x.com"  # id 最小行原名保留
    assert names[1].startswith("orphans@x.com#orphan-")
    assert "uq_billing_customers_name" in _unique_name_indexes(db_url)


def test_migration_downgrade_drops_index_keeps_names(tmp_path, monkeypatch) -> None:
    """downgrade 只删索引不改名（改名不可逆，docstring 如实）；再升回
    往返安全（幂等）。"""
    db_url = f"sqlite:///{tmp_path/'down.db'}"
    monkeypatch.setenv("FD_WIRE_DATABASE_URL", db_url)

    command.upgrade(_alembic_config(), "0036_artifact_marketplace")
    plan_ids = _seed_plans_in(db_url)
    engine = create_engine(db_url)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO billing_customers (name, plan_id, status, created_at)"
            " VALUES ('d@x.com', :free, 'active', CURRENT_TIMESTAMP)",
            {"free": plan_ids["free"]},
        )
    engine.dispose()

    command.upgrade(_alembic_config(), "head")
    command.downgrade(_alembic_config(), "0036_artifact_marketplace")
    assert "uq_billing_customers_name" not in _unique_name_indexes(db_url)
    assert [n for n, _ in _raw_names(db_url)] == ["d@x.com"]

    command.upgrade(_alembic_config(), "head")
    assert "uq_billing_customers_name" in _unique_name_indexes(db_url)


# ---------------------------------------------------------------------------
# 回归守卫：revision id 长度 ≤32（PG alembic_version.version_num varchar(32)；
# SQLite 不强制长度，超长只在生产 PG 暴露——2026-10-15 生产迁移首跑实测）
# ---------------------------------------------------------------------------


def test_all_revision_ids_fit_alembic_version_column() -> None:
    """全链 revision id 必须 ≤32 字符（PG varchar(32) 硬上限）。"""
    import re
    from pathlib import Path

    versions_dir = Path(__file__).resolve().parent.parent / "alembic" / "versions"
    bad = []
    for path in versions_dir.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        m = re.search(r'^revision:\s*str\s*=\s*"([^"]+)"', text, re.MULTILINE)
        if m and len(m.group(1)) > 32:
            bad.append((path.name, m.group(1), len(m.group(1))))
    assert bad == [], f"revision id 超 32 字符（PG 迁移必失败）: {bad}"
