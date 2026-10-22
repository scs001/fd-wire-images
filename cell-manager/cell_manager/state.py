"""生命周期簿记：cells.json（原子写，崩溃不半途）。

记录每 cell 的状态 / 容器名 / 卷路径 / registry 条目路径 / 时间戳；
purge 后记录即删除——不可抵赖的留档在审计日志（audit.py），不在此。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

STATE_VERSION = 1
ACTIVE = "active"
PAUSED = "paused"
RETENTION = "retention"


class StateStore:
    """cells.json 的极简持久层（单进程管理面假设；跨进程互斥由部署面保证）。"""

    def __init__(self, path: Path) -> None:
        self._path = path

    def _load_doc(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"version": STATE_VERSION, "cells": {}}
        try:
            doc = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"corrupt state file {self._path}: {exc}") from exc
        doc.setdefault("version", STATE_VERSION)
        doc.setdefault("cells", {})
        return doc

    def _save_doc(self, doc: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    def get(self, customer_id: str) -> dict[str, Any] | None:
        return self._load_doc()["cells"].get(customer_id)

    def put(self, record: dict[str, Any]) -> None:
        doc = self._load_doc()
        doc["cells"][record["customer_id"]] = record
        self._save_doc(doc)

    def delete(self, customer_id: str) -> None:
        doc = self._load_doc()
        if doc["cells"].pop(customer_id, None) is not None:
            self._save_doc(doc)

    def all_cells(self) -> dict[str, dict[str, Any]]:
        return dict(self._load_doc()["cells"])
