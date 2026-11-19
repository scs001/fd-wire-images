#!/usr/bin/env python3
"""把 fd-wire 已发布的 artifact 版本转成内核事件（change: fuse-marketing-onto-event-kernel 2.1-2.4）。

角色：**只读适配器**。只读 fd-wire 的事实表，经内核唯一写入入口
``fd_erp_ledger_mcp.events.append_event`` 写一条 ``marketing.content.published``
（``lane='ingest'``）。事件层的只追加 / 定型 / 幂等纪律全部由内核负责，
本脚本不直写 SQL —— 否则等于在触发器旁边另开一扇门。

数据形状（以真实 fd-wire 库为准，不是 design 里的简称）：

- ``artifacts`` 持状态机与发布锚点：``status``（draft/published/archived）、
  ``published_at``（进入非 private 档位的时刻）、``published_version``（发布面锚定的版本号）。
- ``artifact_versions`` 是 append-only 的 spec 版本链，**没有** status / published_at
  列（见 ``api/alembic/versions/20261011_0021_artifact_lifecycle.py`` 与
  ``api/tests/test_artifacts_lifecycle.py`` 的列集断言）。因此「已发布的版本行」=
  ``artifacts.status='published' AND artifacts.published_at IS NOT NULL`` 所指产物
  的发布版本锚点（``COALESCE(published_version, version)``）。

幂等键口径：``stable_id = f"{artifact_id}:v{version}"`` —— 用「artifact + 版本」，
不是时间戳。发布是一次可重放的外部可见动作：同一版本重跑多少次都是同一条事实；
用时间戳当键会让每次重放都凭空多一条，把「补跑」变成「造事实」。

回滚行为：版本对应的产物不再是 ``published`` 后，本适配器**不再产生新事实**；
已写的事实不删不改（更正走 ``reverse_event`` 冲正，不归本适配器负责）。

用法：

    python scripts/publish_facts_to_kernel.py --db api/fd_wire_ai.db
    python scripts/publish_facts_to_kernel.py --db api/fd_wire_ai.db --dry-run
    python scripts/publish_facts_to_kernel.py --db api/fd_wire_ai.db --tenant acme

环境变量（不写死 DSN、不落任何凭据）：

- ``ERP_LEDGER_DATABASE_URL``：内核事件库 DSN（``--dry-run`` 不需要）。
- ``ERP_TENANT_ID``：未传 ``--tenant`` 时的租户（与内核 ``Settings`` 同源，默认 default）。
- ``ERP_LEDGER_KERNEL_ROOT``：兄弟仓 ``fd-erp-ledger-mcp`` 路径（默认工作区同级目录）。

这是**可手动跑的脚本**，不进常驻调度：跑一次、可重放（理由见
``docs/publish-facts-to-kernel.md``）。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: 事件类型（已由内核迁移 0011_marketing_producers 登记为 producer='marketing'）。
EVENT_TYPE = "marketing.content.published"
#: 外部事实车道：写入即事实，不需要确认门（发布是外部可见动作，来源即权威）。
LANE = "ingest"
#: 审计里的事件写入者标记。
ACTOR = "fd-wire-publish-adapter"

#: 与内核 ``fd_erp_ledger_mcp.events.UNKNOWN`` 同字面量的一等「显式未知」。
#: 这里不 import 内核常量，是为了让 ``--dry-run`` 在没装内核的机器上也能跑；
#: 两者同值即同语义（内核按「非空字符串」判定，不靠对象同一性）。
UNKNOWN = "__unknown__"

#: 兄弟仓位置：fd-wire/scripts/ → parents[2] = 工作区根。
DEFAULT_KERNEL_ROOT = Path(__file__).resolve().parents[2] / "fd-erp-ledger-mcp"


# ────────────────────────────────────────────────────────────── 读 fd-wire


@dataclass(frozen=True)
class Fact:
    """一条待写事实（已通过状态过滤与载荷装配）。"""

    artifact_id: int
    version: int
    stable_id: str
    occurred_at: datetime
    payload: dict[str, Any]
    source_ref: str


@dataclass(frozen=True)
class Skipped:
    """一条被跳过的产物行（打印用；不写事件）。"""

    artifact_id: int
    version: int | None
    reason: str


#: 每个产物取一行「发布版本」。LEFT JOIN 而非 INNER JOIN：版本行缺失要如实报跳过，
#: 不能被 JOIN 静默吞掉（否则「扫描数」会说谎）。
_READ_SQL = """
SELECT a.id                           AS artifact_id,
       a.title                        AS artifact_title,
       a.status                       AS artifact_status,
       a.published_at                 AS artifact_published_at,
       a.customer_id                  AS customer_id,
       v.id                           AS version_row_id,
       v.version                      AS version,
       v.spec                         AS version_spec
  FROM artifacts a
  LEFT JOIN artifact_versions v
         ON v.artifact_id = a.id
        AND v.version = COALESCE(a.published_version, a.version)
 ORDER BY a.id
"""


def _parse_dt(value: Any) -> datetime:
    """SQLite 的 DATETIME 读出来是字符串（无时区）；fd-wire 一律按 UTC 存。"""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _spec_slug(spec: Any) -> str | None:
    """取 ``spec.spec.showcase_slug``（与 ``artifacts/service.py::_spec_slug`` 同口径）。

    不在这里另造 slug 规则：slug 是 fd-wire 的发布面概念，适配器只读不解释。
    """
    if not isinstance(spec, dict):
        return None
    inner = spec.get("spec")
    if not isinstance(inner, dict):
        return None
    slug = inner.get("showcase_slug")
    return slug if isinstance(slug, str) and slug else None


def _load_spec(value: Any) -> dict[str, Any]:
    """SQLite 的 JSON 列读出来是文本；坏数据按「无 spec」处理，不猜内容。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes)) and value:
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _spec_title(spec: Any) -> str | None:
    if not isinstance(spec, dict):
        return None
    title = spec.get("title")
    return title if isinstance(title, str) and title.strip() else None


def collect(db_path: str | Path) -> tuple[list[Fact], list[Skipped]]:
    """读 fd-wire SQLite，返回 (待写事实, 跳过行)。

    只读打开（``mode=ro``）：适配器没有改 fd-wire 数据的路径。
    """
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"fd-wire 数据库不存在：{path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(_READ_SQL).fetchall()
    finally:
        conn.close()

    facts: list[Fact] = []
    skipped: list[Skipped] = []
    for row in rows:
        artifact_id = int(row["artifact_id"])
        version = int(row["version"]) if row["version"] is not None else None
        status = str(row["artifact_status"] or "")

        # 状态过滤：只有「仍处于 published」的产物才产生新事实。
        # 这是回滚语义的判据 —— published_at 在回滚后仍留在产物行上（归档/恢复不清时间戳），
        # 只按 published_at 过滤会把已回滚的版本重新写成事实。
        if status != "published":
            skipped.append(Skipped(
                artifact_id, version, f"status={status}（非 published，已回滚/未发布）"))
            continue
        if row["artifact_published_at"] in (None, ""):
            skipped.append(Skipped(
                artifact_id, version, "published_at 为空（尚未对外发布）"))
            continue
        if version is None or row["version_row_id"] is None:
            skipped.append(Skipped(
                artifact_id, None, "发布版本行缺失（artifact_versions 无对应 version）"))
            continue

        published_at = _parse_dt(row["artifact_published_at"])
        spec = _load_spec(row["version_spec"])
        title = (str(row["artifact_title"] or "").strip()
                 or _spec_title(spec) or "")
        if not title:
            skipped.append(Skipped(artifact_id, version, "title 为空（无法表达发布事实）"))
            continue

        source_ref = f"fd-wire:artifact:{artifact_id}:v{version}"
        payload = {
            # slug 是发布面身份；缺失时写内核的一等未知值，不猜、不拼。
            "slug": _spec_slug(spec) or UNKNOWN,
            "title": title,
            "published_at": _iso(published_at),
            "artifact_ref": source_ref,
            # 附加定位字段（内核只校验 required，多写不拒）：
            "version": version,
            "customer_id": row["customer_id"],
        }
        facts.append(Fact(
            artifact_id=artifact_id,
            version=version,
            # 幂等键：artifact + 版本。重放同一版本不产生第二条。
            stable_id=f"{artifact_id}:v{version}",
            occurred_at=published_at,
            payload=payload,
            source_ref=source_ref,
        ))
    return facts, skipped


# ────────────────────────────────────────────────────────────── 写内核


def _kernel():
    """惰性 import 兄弟仓内核（dry-run / 只读校验路径不需要它）。"""
    root = Path(os.environ.get("ERP_LEDGER_KERNEL_ROOT") or DEFAULT_KERNEL_ROOT)
    if not root.is_dir():
        raise RuntimeError(
            f"内核仓不在位：{root}（用 ERP_LEDGER_KERNEL_ROOT 指定 fd-erp-ledger-mcp 路径）"
        )
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from fd_erp_ledger_mcp import events  # noqa: PLC0415
    from fd_erp_ledger_mcp.config import Settings  # noqa: PLC0415
    return events, Settings


@dataclass(frozen=True)
class Outcome:
    kind: str          # written | duplicate | dry-run | skipped
    artifact_id: int
    version: int | None
    stable_id: str | None = None
    detail: str = ""


_LABEL = {
    "written": "已写  ",
    "duplicate": "已存在",
    "dry-run": "将写  ",
    "skipped": "跳过  ",
}


def _print_outcome(outcome: Outcome) -> None:
    line = _LABEL[outcome.kind]
    if outcome.artifact_id is not None:
        version = "-" if outcome.version is None else f"v{outcome.version}"
        line += f" artifact={outcome.artifact_id} {version}"
    if outcome.stable_id:
        line += f" stable_id={outcome.stable_id}"
    if outcome.detail:
        line += f" {outcome.detail}"
    print(line)


def run(
    db_path: str | Path,
    *,
    tenant_id: str | None = None,
    dry_run: bool = False,
    settings: Any | None = None,
) -> dict[str, Any]:
    """读 fd-wire → 写内核。返回汇总（测试直接断言这个结构）。

    ``settings`` 可注入（测试用）；缺省时从 ``ERP_LEDGER_DATABASE_URL`` 构造，
    DSN 绝不硬编码、绝不落盘。``dry_run`` 只打印，不建内核连接。
    """
    facts, skipped = collect(db_path)
    tenant = (
        tenant_id
        or getattr(settings, "tenant_id", None)
        or os.environ.get("ERP_TENANT_ID")
        or "default"
    )
    outcomes: list[Outcome] = [
        Outcome("skipped", s.artifact_id, s.version, detail=s.reason) for s in skipped
    ]

    if dry_run:
        outcomes += [
            Outcome("dry-run", f.artifact_id, f.version, f.stable_id) for f in facts
        ]
    elif facts:
        events, Settings = _kernel()
        if settings is None:
            dsn = os.environ.get("ERP_LEDGER_DATABASE_URL")
            if not dsn:
                raise RuntimeError(
                    "未配置内核数据源：缺少环境变量 ERP_LEDGER_DATABASE_URL"
                    "（只想看将写什么请加 --dry-run）"
                )
            settings = Settings(database_url=dsn, tenant_id=tenant)

        # 连接自备而非用内核的 get_pool：fd-wire 的运行环境没有 psycopg-pool，
        # 而 append_event 只要求一个 psycopg3 连接（autocommit，事务由内核自己开）。
        import psycopg  # noqa: PLC0415

        with psycopg.connect(settings.database_url, autocommit=True) as conn:
            for fact in facts:
                view = events.append_event(
                    conn,
                    settings=settings,
                    tenant_id=tenant,
                    event_type=EVENT_TYPE,
                    stable_id=fact.stable_id,
                    occurred_at=fact.occurred_at,
                    payload=fact.payload,
                    lane=LANE,
                    source_ref=fact.source_ref,
                    actor=ACTOR,
                )
                duplicated = bool(view.get("duplicate"))
                outcomes.append(Outcome(
                    "duplicate" if duplicated else "written",
                    fact.artifact_id,
                    fact.version,
                    fact.stable_id,
                    detail="(幂等命中，未新增)" if duplicated else "",
                ))

    for outcome in outcomes:
        _print_outcome(outcome)

    written = sum(1 for o in outcomes if o.kind == "written")
    duplicate = sum(1 for o in outcomes if o.kind == "duplicate")
    planned = sum(1 for o in outcomes if o.kind == "dry-run")
    skipped_n = sum(1 for o in outcomes if o.kind == "skipped")
    summary = {
        "scanned": len(facts) + skipped_n,
        "facts": len(facts),
        "written": written,
        "duplicate": duplicate,
        "skipped": skipped_n,
        "planned": planned,
        "dry_run": dry_run,
        "tenant": tenant,
    }
    tag = "（dry-run：未写入）" if dry_run else ""
    print(
        f"汇总{tag}: 扫描 {summary['scanned']} 个产物 · 待写 {summary['facts']} · "
        f"已写 {written} · 已存在 {duplicate} · 跳过 {skipped_n}"
    )
    return {**summary, "outcomes": outcomes}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="fd-wire 已发布 artifact 版本 → 内核 marketing.content.published 事件")
    parser.add_argument("--db", required=True, help="fd-wire 的事实库（SQLite 路径，只读）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将写什么，不写内核、不要 DSN")
    parser.add_argument("--tenant", default=None,
                        help="内核租户 id（默认取 ERP_TENANT_ID，再默认 default）")
    args = parser.parse_args(argv)

    try:
        run(args.db, tenant_id=args.tenant, dry_run=args.dry_run)
    except FileNotFoundError as exc:
        print(f"输入错误：{exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # 内核拒绝（ErpError）原样带出错误码，不改写成成功
        code = getattr(exc, "code", None) or type(exc).__name__
        print(f"内核拒绝：{code} {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
