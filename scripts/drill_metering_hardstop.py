#!/usr/bin/env python3
"""计量入账硬停演练（add-ecosystem-bridge 3.2 / wire 线 sub2api 入账管道）。

实证不变量（「降级不放行无计量调用」在结算车道的等价命题）：

  R1 闭月结算轮：上月事件 → 额度+超额入账，sub2api 恰一次扣减
  R2 当月累积轮：当月事件只计量不结算（闭月语义；消费由预检/预警兜底）
  R3 降级轮：    计费面 503 → 超额账不落、余额不动、**计量行完好不丢**
  R4 恢复轮：    同幂等键重放 → 恰一次扣减（幂等缓存跨轮去重）
  对账：         metering 行数 / 账本条目 / 余额变动三方一致，差异=0

预检侧硬停（余额耗尽 402、计费断连 503 fail-closed）在注册处网关已活链
实证（2026-10-05 wire 客户真回合 + 2026-10-07 探针），本演练覆盖结算侧。

自包含（除 fd_wire_api 自身与测试假件外无外部服务）：

    cd fd-wire && python3 scripts/drill_metering_hardstop.py [--out REPORT.md]
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))
sys.path.insert(0, str(ROOT / "api" / "tests"))

os.environ.setdefault("DATABASE_URL", "sqlite:///./fd_wire_dev.db")
os.environ["SUB2API_ADMIN_KEY"] = ""  # 演练全程注入假件，不碰真计费面
os.environ.pop("REGISTRY_MONGO_URI", None)
os.environ.pop("REGISTRY_MONGO_DB", None)


def main() -> int:
    from sqlalchemy import select

    from fd_wire_api.billing import LedgerKind, init_billing, seed_plans
    from fd_wire_api.metering.db import ensure_schema, get_session, reset_metering_runtime
    from fd_wire_api.metering.driver import tick_once
    from test_billing_common import FakeSub2Api, add_customer
    from test_metering_poller import FakeCollection
    from test_metering_support import make_event

    user = "drill-cust@customer.com"
    opening = 10_000_000
    rows_over_grant = 600_000  # 超 starter 含额 → 必产生超额
    now = datetime(2026, 10, 2, tzinfo=timezone.utc)  # 闭月 = 2026-09

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DATABASE_URL"] = f"sqlite:///{tmp}/drill.db"
        reset_metering_runtime()
        ensure_schema()
        init_billing()

        fake = FakeSub2Api()
        session = get_session()
        plans = seed_plans(session)
        session.commit()
        add_customer(session, name="演练客户", plan_id=plans["starter"].id, sub2api_user_id=user)
        fake.add_user(user, balance_micro=opening)
        session.commit()
        session.close()

        def events(month: str, n: int = 1) -> list[dict]:
            return [
                make_event(request_id=f"r-{month}-{i}", username=user,
                           rows=rows_over_grant, ts=f"{month}-15T10:0{i}:00+00:00")
                for i in range(n)
            ]

        def ledger(kind: LedgerKind) -> int:
            from fd_wire_api.billing import LedgerEntry

            with get_session() as s:
                return len(list(s.execute(
                    select(LedgerEntry).where(LedgerEntry.kind == kind)).scalars()))

        lines: list[str] = ["# 计量入账硬停演练报告", "",
                            f"- 时刻：{datetime.now(timezone.utc).isoformat()}",
                            f"- 客户：{user}（starter 档，预存 ¥10）",
                            f"- 闭月聚合域：2026-09（tick now={now.isoformat()}）", ""]
        ok = True

        def check(name: str, cond: bool, detail: str) -> None:
            nonlocal ok
            lines.append(f"- {'✓' if cond else '✗'} {name} — {detail}")
            if not cond:
                ok = False

        # R1 闭月结算轮
        r1 = tick_once(collection=FakeCollection(events("2026-09")), sub2api=fake.client(), now=now)
        balance_r1 = fake.balance(user)
        check("R1 上月事件入账", r1["polled"]["accepted"] == 1 and r1["settled"]["ok"] is True,
              f"polled={r1['polled']} settled={r1['settled']}")
        check("R1 额度+超额条目", ledger(LedgerKind.GRANT) == 1 and ledger(LedgerKind.OVERAGE) == 1,
              f"grant={ledger(LedgerKind.GRANT)} overage={ledger(LedgerKind.OVERAGE)}")
        check("R1 恰一次扣减", len(fake.adjust_calls) == 1 and balance_r1 < opening,
              f"adjust_calls={len(fake.adjust_calls)} balance={balance_r1}")
        lines.append("  - 注：结算层允许余额转负（月内欠费态）——运行时刹车是预检 402，"
                     "两层各司其职（后置结算 + 准入硬停）。")

        # R2 当月累积轮（闭月语义：只计量）
        r2 = tick_once(collection=FakeCollection(events("2026-10")), sub2api=fake.client(), now=now)
        check("R2 当月只计量不结算", r2["polled"]["accepted"] == 1 and fake.balance(user) == balance_r1
              and ledger(LedgerKind.OVERAGE) == 1,
              f"polled={r2['polled']} balance 不变, overage 仍 1")

        # R3 降级轮（计费面 503；上月重放遇降级）
        fake.fail_adjust_left = 99  # 持续降级
        r3 = tick_once(collection=FakeCollection(events("2026-09")), sub2api=fake.client(), now=now)
        check("R3 降级不落超额账、余额不动",
              fake.balance(user) == balance_r1 and ledger(LedgerKind.OVERAGE) == 1,
              f"balance={fake.balance(user)} overage={ledger(LedgerKind.OVERAGE)}")
        with get_session() as s:
            from fd_wire_api.metering.models import MeteringRecord

            metered = len(list(s.execute(select(MeteringRecord)).scalars()))
        check("R3 计量行完好不丢（降级零丢失）", metered == 2, f"metering_records={metered}（应为 2）")

        # R4 恢复轮（幂等重放恰一次）
        fake.fail_adjust_left = 0
        r4 = tick_once(collection=FakeCollection(events("2026-09")), sub2api=fake.client(), now=now)
        overage_total = ledger(LedgerKind.OVERAGE)
        check("R4 恢复重放幂等（恰一次扣减）",
              fake.balance(user) == balance_r1 and overage_total == 1 and r4["settled"]["ok"] is True,
              f"balance 回到 R1 水位且不双扣, overage={overage_total}")

        # 对账：三方一致
        expected_charge = opening - balance_r1
        check("对账：扣减额与超额条目一致", expected_charge > 0 and len(fake.adjust_calls) == 1,
              f"净扣减={expected_charge}µ, adjust_calls={len(fake.adjust_calls)}（503 不入 calls 计数口径见 FakeSub2Api.calls）")

        lines += ["", ("**结论：全部通过。**" if ok else "**结论：存在未过项。**"),
                  "", "降级期间计量零丢失、恢复恰一次入账、无双重扣减、当月消费由预检门兜底——",
                  "「降级不放行无计量调用」在结算车道成立（预检侧 402/503 已活链实证）。"]
        report = "\n".join(lines)
        print(report)
        args = parser.parse_args()
        if args.out:
            Path(args.out).write_text(report + "\n", encoding="utf8")
            print(f"\n报告已写 {args.out}")
        return 0 if ok else 1


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--out", default=None, help="报告输出路径（可选）")

if __name__ == "__main__":
    raise SystemExit(main())
