"""审计日志：JSONL append-only（时间 / 操作 / 客户 / 结果）。

注销到期 purge 的不可抵赖留档即在此——state 可删、卷可删，审计行不删。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    """当前 UTC 时间的 ISO-8601（秒级 + Z 后缀，审计行排序友好）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AuditLog:
    """JSONL 审计日志（每行一 JSON 对象；每次 append 即 flush+close，崩溃安全）。"""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def append(
        self,
        *,
        action: str,
        customer_id: str,
        result: str,
        detail: dict[str, Any] | None = None,
        ts: str | None = None,
    ) -> dict[str, Any]:
        """追加一行审计记录并返回该记录（便于调用方透传给 CLI 输出）。"""
        entry: dict[str, Any] = {
            "ts": ts or utc_now_iso(),
            "action": action,
            "customer_id": customer_id,
            "result": result,
        }
        if detail:
            entry["detail"] = detail
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def read_all(self) -> list[dict[str, Any]]:
        """读回全部审计行（测试与后台展示用）。"""
        if not self._path.exists():
            return []
        lines = self._path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]
