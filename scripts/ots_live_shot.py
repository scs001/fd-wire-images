#!/usr/bin/env python3
"""OTS 主网实弹验收（wire-provenance-ledger 任务 5.3 / 10.1 点亮证据）。

对两个不同 Merkle 根各提交一次 OpenTimestamps 锚定（官方公共 calendar 池），
用 anchors.OtsBackend.verify_local 独立核验回执：
1. external_ref = base64(OTS 凭据) 且可解析（DetachedTimestampFile）；
2. 凭据承诺摘要 == 提交根（回执不可冒充）；
3. 含 calendar attestation（BTC 确认异步补齐，pending 属预期态）。

产出：openspec/changes/wire-provenance-ledger/reports/ots-live-shot.md。
"""

from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "api"))

from fd_wire_api.provenance.anchors import ots_backend  # noqa: E402

REPORT = (
    REPO.parent / "openspec" / "changes" / "wire-provenance-ledger" / "reports" / "ots-live-shot.md"
)


def main() -> int:
    lines: list[str] = [
        "# OTS 主网实弹记录（任务 5.3 验收证据）",
        "",
        f"- 时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "- 通道：官方公共 calendar 聚合池（DEFAULT_AGGREGATORS，真实网络往返）",
        "",
    ]
    ok_all = True
    for i, seed in enumerate(("wire-provenance-ledger-acceptance-root-a", "root-b-柏讯溯源"), start=1):
        root = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        section_ok = False
        try:
            receipt = ots_backend.submit(f"acceptance-{i}", root, {"epoch": i, "note": "live shot"})
            verify = ots_backend.verify_local(
                {"external_ref": receipt["external_ref"], "merkle_root": root}
            )
            section_ok = verify["status"] in ("pending", "verified")
            lines += [
                f"## 提交 {i}",
                f"- 提交根：`{root}`",
                f"- 回执：backend={receipt['backend']} ts_external={receipt['ts_external']}"
                f" external_ref=base64({len(receipt['external_ref'])} 字符)",
                f"- 独立核验（verify_local）：status={verify['status']}"
                f" detail={verify['detail']}",
                "",
            ]
        except Exception as exc:  # noqa: BLE001
            lines += [f"## 提交 {i}：FAIL（{type(exc).__name__}: {exc}）", ""]
        ok_all = ok_all and section_ok
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(
        "\n".join(lines) + f"\n结论：{'通过（pending 属预期——BTC 确认异步补齐）' if ok_all else '未通过'}\n",
        encoding="utf-8",
    )
    print(f"[{'PASS' if ok_all else 'FAIL'}] 证据已写入 {REPORT}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
