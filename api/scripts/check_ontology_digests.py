#!/usr/bin/env python3
"""只读核对：库里每一份本体快照的 ``digest`` 是否等于按 manifest 重算的值。

**为什么需要它**：``ontology_snapshots.digest`` 是导出物完整性校验值，消费方按
它判断「这份 manifest 与发布时一致」。换底把 digest 交给内核算
（establish-ontology-kernel 6.2），换完必须对**库里已有的每一行**重算一遍——
本地开发库通常是 0 行，那就等于没验证过；只有对着真有数据的库跑，才知道
换底有没有在历史版本上悄悄改了 digest。

**只读**：只 SELECT，不建表、不写行、不碰 alembic。

用法::

    DATABASE_URL=postgresql+psycopg://... python scripts/check_ontology_digests.py
    python scripts/check_ontology_digests.py --database sqlite:///./fd_wire_entities.db
    python scripts/check_ontology_digests.py --limit 50

退出码：``0`` 全部相等（或库里没有快照——会明确说「没有样本，本次未验证」），
``1`` 有不相等的行，``2`` 跑不起来（连不上库/表不存在/参数错）。

输出形态：每条不一致一行 ``<customer> · v<version> · 事实 · 修法：...``，最后一行
汇总。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session


def classify(manifest, stored: str, recomputed: str, *, pinned: str | None = None) -> str:
    """``ok`` / ``legacy-known`` / ``legacy`` / ``mismatch``——四态，别把它们混成两种。

    ``legacy``：这行的 digest 是**补键规则之前**铸造的。0050 给实体/关系加了
    ``tier``/``sensitivity``，归一器从此按「缺键 → null」补齐（design D5），于是
    对 0050 之前的快照，「先归一再哈希」必然复现不出当年那个值。判据是**原始
    payload 直接哈希**是否等于库中值：等于就说明当年正是按原始 payload 铸的。

    这类行在换底前后**都**对不上（旧 ``normalize_entity`` 同样补这两个键），所以
    它既不是回归、也改代码救不了；更**不能**改库里的 digest——消费方已按它校验过
    导出物。

    ``legacy-known`` 与 ``legacy`` 是**两种结果**：只有出现在仓里的遗留名单
    （``legacy_ontology_digests.txt``，逐行钉 customer/version/当时的 digest）的那一行
    才算「已知、可放行」。没钉过的遗留行仍然是失败——否则一个全局开关会顺带放过
    将来任何新出现的遗留行，那就又变成「沉默即通过」了。

    ``mismatch``：原始 payload 也对不上。这才是真问题：配方被改过，或这一行被改写过。
    """
    if recomputed == stored:
        return "ok"
    raw = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        .encode("utf-8")
    ).hexdigest()
    if raw != stored:
        return "mismatch"
    # 钉住的必须是**同一个 digest**：同一个 (customer, version) 若被改写过，
    # 库里的值变了，钉的那条就对不上，仍然算未解释的遗留。
    return "legacy-known" if pinned == stored else "legacy"


def load_pins(path: Path | None) -> dict[tuple[str, int], str]:
    """读遗留名单：``customer<TAB>version<TAB>digest<TAB>原因``（``#`` 开头为注释）。"""
    if path is None or not path.exists():
        return {}
    pins: dict[tuple[str, int], str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = raw.split("\t")
        if len(parts) < 3:
            continue
        customer, version, digest = parts[0].strip(), parts[1].strip(), parts[2].strip()
        pins[(customer, int(version))] = digest
    return pins


def _engine(database: str | None):
    from fd_wire_api.config import normalize_database_url

    url = database or os.getenv("FD_WIRE_DATABASE_URL") or os.getenv(
        "FD_WIRE_ENTITIES_DATABASE_URL"
    )
    if not url:
        print(
            "快照核对 · 参数 · 没有数据库连接串 · "
            "修法：传 --database 或设 DATABASE_URL / FD_WIRE_DATABASE_URL",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return create_engine(normalize_database_url(url), future=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_ontology_digests",
        description="Recompute every ontology snapshot digest and compare with the stored value.",
    )
    parser.add_argument("--database", default=None,
                        help="database URL (default: DATABASE_URL / FD_WIRE_DATABASE_URL)")
    parser.add_argument("--limit", type=int, default=0,
                        help="stop after N rows (0 = all)")
    parser.add_argument(
        "--legacy-pins",
        default=str(Path(__file__).with_name("legacy_ontology_digests.txt")),
        help="遗留 digest 名单（逐行钉 customer/version/digest）；默认取脚本同目录那份",
    )
    parser.add_argument(
        "--allow-legacy",
        action="store_true",
        help="把**未钉进名单**的遗留行也放行（应急开关；正常应改名单而不是开它）",
    )
    args = parser.parse_args(argv)

    engine = _engine(args.database)
    try:
        # 先导 app：datasets 包在 import 期有循环引用（entities → datasets → ai →
        # materialization → datasets.ingest），单独导 fd_wire_api.entities 会炸在
        # 「partially initialized module」上。测试套件同样是先导 app 才成立。
        import fd_wire_api.app  # noqa: F401  仅为打平导入顺序
        from fd_wire_api.entities import versioning
        from fd_wire_api.entities.models import OntologySnapshot
    except Exception as exc:  # noqa: BLE001 - 环境问题必须说清楚，别抛栈给运维
        print(
            f"快照核对 · 导入 · {type(exc).__name__}: {exc} · "
            f"修法：在 api/ 目录下用本仓 venv 运行（缺依赖或包路径不对）",
            file=sys.stderr,
        )
        return 2

    checked = 0
    mismatches: list[tuple[str, int, str, str]] = []
    legacy: list[tuple[str, int, str, str]] = []
    known: list[tuple[str, int, str, str]] = []
    pins = load_pins(Path(args.legacy_pins) if args.legacy_pins else None)
    try:
        with Session(engine) as session:
            stmt = select(OntologySnapshot).order_by(
                OntologySnapshot.customer_id, OntologySnapshot.version
            )
            if args.limit:
                stmt = stmt.limit(args.limit)
            for snapshot in session.scalars(stmt).all():
                manifest = snapshot.manifest_json or {}
                recomputed = versioning.manifest_digest(manifest)
                checked += 1
                verdict = classify(
                    manifest,
                    snapshot.digest,
                    recomputed,
                    pinned=pins.get((snapshot.customer_id, snapshot.version)),
                )
                row = (snapshot.customer_id, snapshot.version, snapshot.digest, recomputed)
                if verdict == "legacy-known":
                    known.append(row)
                elif verdict == "legacy":
                    legacy.append(row)
                elif verdict == "mismatch":
                    mismatches.append(row)
    except Exception as exc:  # noqa: BLE001
        print(
            f"快照核对 · 连接/查询 · {type(exc).__name__}: {exc} · "
            f"修法：确认库可达且已跑过 alembic（ontology_snapshots 表存在）",
            file=sys.stderr,
        )
        return 2

    for customer, version, stored, recomputed in known:
        print(
            f"{customer} · v{version} · LEGACY(已知) · 库中 digest={stored} 重算={recomputed} · "
            f"已钉进 legacy_ontology_digests.txt，按遗留处理"
        )

    for customer, version, stored, recomputed in legacy:
        print(
            f"{customer} · v{version} · LEGACY(未钉) · 库中 digest={stored} 重算={recomputed} · "
            f"该行 digest 由「缺键补 null」规则之前的版本铸造（原始 payload 哈希才等于库中值）；"
            f"确认无误后把它钉进 legacy_ontology_digests.txt（带原因），别开 --allow-legacy"
        )

    for customer, version, stored, recomputed in mismatches:
        print(
            f"{customer} · v{version} · 库中 digest={stored} 重算={recomputed} · "
            f"修法：先确认换底是否改了归一/序列化配方（golden 测试应拦下）；"
            f"**不要**改库里的 digest，那会作废消费方已校验过的导出物"
        )

    if mismatches:
        print(f"\nFAILED: {len(mismatches)}/{checked} 份快照 digest 不一致"
              + (f"（另有 {len(known)} 份已知遗留、{len(legacy)} 份未钉遗留）" if known or legacy else ""))
        return 1

    if legacy and not args.allow_legacy:
        # 未钉的遗留行不是「验过」——不处理掉就仍然算没通过，别让沉默变成通过。
        print(
            f"\nLEGACY: {len(legacy)}/{checked} 份快照的 digest 由补键规则之前铸造且未钉进名单；"
            f"确认后写进 legacy_ontology_digests.txt 再跑"
        )
        return 1

    if known or legacy:
        explained = len(known) + (len(legacy) if args.allow_legacy else 0)
        print(f"（其中 {explained} 份为遗留 digest，已按名单/应急开关放行）")

    if checked == 0:
        # 「没有样本」不等于「验证通过」——这两种结果必须长得不一样。
        print("NO SAMPLES: ontology_snapshots 里没有行，本次未验证任何历史 digest")
        return 0

    print(f"PASS: {checked} 份快照 digest 全部与重算值相等")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
