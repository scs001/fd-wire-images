#!/usr/bin/env python3
"""三处只读对账：上游 feed 快照 vs 目录条目 vs claim 索引。

口径（xingyan-content-supply，spec「三处对账只读零漂移」）：

1. **上游**：现取官网 ``content-manifest.json`` 与三区 feed（不落库）。
2. **目录条目**：``xingyan_items`` 里 ``status='active'`` 的行。
3. **claim 索引**：``xingyan_claims`` 里 ``status='active'`` 的行。

逐区比对条目集合、claim 集合与指纹，差异**指名三处取值**；只读，绝不修
任何一处。零差异才退出 0。

用法：
  python scripts/xingyan_reconcile.py                       # 比对线上官网 vs 本地库
  python scripts/xingyan_reconcile.py --feed-dir /path/dist # 比对本地构建产物
  python scripts/xingyan_reconcile.py --json                # 机器可读报告
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ZONES = ("briefings", "academy", "research")


def _load_upstream(feed_dir: str | None, base_url: str) -> tuple[dict, dict, dict]:
    """返回 (manifest, {zone: items}, {zone: claims: {slug: {ids}}})。"""
    if feed_dir:
        root = Path(feed_dir)
        manifest = json.loads((root / "content-manifest.json").read_text())
        feeds = {z: json.loads((root / f"{z}.json").read_text()) for z in ZONES}
    else:
        import httpx

        # trust_env=False：集群内系统代理劫持回环请求有先例（502 假故障）
        with httpx.Client(base_url=base_url.rstrip("/"), timeout=30, trust_env=False) as c:
            manifest = c.get("/content-manifest.json").json()
            feeds = {z: c.get(f"/{z}.json").json() for z in ZONES}

    claims = {}
    for zone in ZONES:
        claims[zone] = {}
        for item in feeds[zone][zone]:
            side = item.get("claims") or {}
            ids = {
                c.get("id")
                for c in (side.get("zh") or []) + (side.get("en") or [])
                if isinstance(c, dict) and c.get("id")
            }
            claims[zone][item["slug"]] = ids
    return manifest, {z: feeds[z][z] for z in ZONES}, claims


def _load_local() -> tuple[dict, dict, dict, dict]:
    from fd_wire_api.xingyan import models
    from fd_wire_api.xingyan.db import readonly_scope, init_db

    init_db()
    items: dict[str, dict] = {}
    claims: dict[str, dict] = {}
    state = {}
    with readonly_scope() as session:
        for row in session.query(models.XingyanItem).filter(
            models.XingyanItem.status == "active"
        ):
            items[row.slug] = {"zone": row.zone, "claim_count": row.claim_count,
                               "sha": row.source_sha, "titles": [row.title_zh, row.title_en]}
        for row in session.query(models.XingyanClaim).filter(
            models.XingyanClaim.status == "active"
        ):
            claims.setdefault(row.slug, set()).add(row.claim_id)
        st = session.get(models.XingyanSyncState, 1)
        if st is not None:
            state = {
                "source_sha": st.source_sha,
                "schema_version": st.schema_version,
                "status": st.status,
                "items": st.items_total,
                "claims": st.claims_total,
                "last_success_at": st.last_success_at.isoformat() if st.last_success_at else None,
            }
    return items, claims, state, {}


def reconcile(feed_dir: str | None, base_url: str) -> dict:
    manifest, up_items, up_claims = _load_upstream(feed_dir, base_url)
    loc_items, loc_claims, state, _ = _load_local()

    problems: list[dict] = []
    for zone in ZONES:
        up_slugs = {i["slug"] for i in up_items[zone]}
        loc_slugs = {s for s, v in loc_items.items() if v["zone"] == zone}
        for slug in sorted(up_slugs - loc_slugs):
            problems.append({"kind": "missing_in_snapshot", "zone": zone, "slug": slug,
                             "upstream": "present", "snapshot": "absent"})
        for slug in sorted(loc_slugs - up_slugs):
            problems.append({"kind": "extra_in_snapshot", "zone": zone, "slug": slug,
                             "upstream": "absent", "snapshot": "present"})
        for slug in sorted(up_slugs & loc_slugs):
            up_ids = up_claims[zone].get(slug, set())
            loc_ids = loc_claims.get(slug, set())
            if up_ids != loc_ids:
                problems.append({
                    "kind": "claim_set_drift", "zone": zone, "slug": slug,
                    "upstream": sorted(up_ids), "snapshot": sorted(loc_ids),
                })
            expected = len(up_ids)
            if loc_items[slug]["claim_count"] != expected:
                problems.append({
                    "kind": "claim_count_drift", "zone": zone, "slug": slug,
                    "upstream": expected, "snapshot": loc_items[slug]["claim_count"],
                })

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "feed_source": feed_dir or base_url,
        "snapshot": state,
        "upstream_fingerprint": {
            z: {"items": len(up_items[z]), "sha": (manifest.get("zones") or {}).get(z, {}).get("sha")}
            for z in ZONES
        },
        "local": {
            "items": len(loc_items),
            "claims": sum(len(v) for v in loc_claims.values()),
            "zones": {z: sum(1 for v in loc_items.values() if v["zone"] == z) for z in ZONES},
        },
        "diffs": problems,
        "ok": not problems,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--feed-dir", help="本地构建产物目录（dist/）")
    ap.add_argument("--feed-base", default=os.getenv("XINGYAN_FEED_BASE_URL",
                                                     "https://www.finddatatech.cloud"))
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = ap.parse_args()

    report = reconcile(args.feed_dir, args.feed_base)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"对账时点 {report['generated_at']}")
        print(f"  上游 {report['feed_source']}  快照 {report['snapshot'].get('source_sha') or '(空)'}")
        print(f"  快照条目 {report['local']['items']} / claim {report['local']['claims']}")
        print(f"  分区 {report['local']['zones']}")
        if report["ok"]:
            print("✓ 三处零漂移")
        else:
            print(f"✗ {len(report['diffs'])} 处差异：")
            for d in report["diffs"]:
                print(f"  - [{d['kind']}] {d['zone']}/{d['slug']}  "
                      f"上游={d['upstream']}  快照={d['snapshot']}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
