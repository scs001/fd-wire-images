"""FakeRegistryClient：内存态 registry（测试 / 无网演示）。

可选 ``persist_path``：条目表落盘（JSON）——CLI 跨进程演示时与
cells.json 簿记、FakeDriver 容器表同步存续（单测缺省纯内存）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from cell_manager.errors import RegistryError
from cell_manager.registry.base import ScopeDoc, ServerEntry


class FakeRegistryClient:
    """path → ServerEntry 表 + scope 文档表 + 调用记录；register/upsert 幂等、
    unregister/delete_scope 宽松。"""

    def __init__(self, persist_path: Path | str | None = None) -> None:
        self._persist_path = Path(persist_path) if persist_path is not None else None
        #: 全部调用轨迹（不落盘）：
        #: ("register", path) | ("unregister", path) | ("upsert_scope", scope_name)
        #: | ("delete_scope", scope_name)
        self.calls: list[tuple[str, str]] = []
        self._entries: dict[str, ServerEntry] = {}
        self._scopes: dict[str, ScopeDoc] = {}
        self.caller_map: dict[str, str] = {}
        self._load()

    def register(self, entry: ServerEntry) -> None:
        self.calls.append(("register", entry.path))
        existing = self._entries.get(entry.path)
        if existing is not None and existing.target_url != entry.target_url:
            raise RegistryError(
                f"path {entry.path!r} already registered to {existing.target_url!r}"
            )
        self._entries[entry.path] = entry
        self._save()

    def unregister(self, path: str) -> bool:
        self.calls.append(("unregister", path))
        removed = self._entries.pop(path, None) is not None
        self._save()
        return removed

    def upsert_scope(self, scope: ScopeDoc) -> None:
        self.calls.append(("upsert_scope", scope.scope_name))
        self._scopes[scope.scope_name] = scope
        self._save()

    def delete_scope(self, scope_name: str) -> bool:
        self.calls.append(("delete_scope", scope_name))
        removed = self._scopes.pop(scope_name, None) is not None
        self._save()
        return removed

    def put_caller_map(self, username: str, value: str) -> None:
        self.calls.append(("put_caller_map", username))
        self.caller_map[username] = value

    def delete_caller_map(self, username: str) -> bool:
        self.calls.append(("delete_caller_map", username))
        return self.caller_map.pop(username, None) is not None

    # ---- 测试辅助 ----

    def is_registered(self, path: str) -> bool:
        return path in self._entries

    def entry(self, path: str) -> ServerEntry | None:
        return self._entries.get(path)

    @property
    def paths(self) -> list[str]:
        return sorted(self._entries)

    def scope(self, scope_name: str) -> ScopeDoc | None:
        return self._scopes.get(scope_name)

    @property
    def scope_names(self) -> list[str]:
        return sorted(self._scopes)

    # ---- 落盘（可选） ----

    def _load(self) -> None:
        if self._persist_path is None or not self._persist_path.exists():
            return
        doc = json.loads(self._persist_path.read_text(encoding="utf-8"))
        for item in doc.get("entries", []):
            entry = ServerEntry(
                path=item["path"],
                target_url=item["target_url"],
                display_name=item.get("display_name"),
                visibility=item.get("visibility", "public"),
                allowed_groups=tuple(item.get("allowed_groups") or ()),
            )
            self._entries[entry.path] = entry
        for item in doc.get("scopes", []):
            scope = ScopeDoc(
                scope_name=item["scope_name"],
                server=item["server"],
                group=item["group"],
                methods=tuple(item.get("methods") or ("all",)),
                tools=tuple(item.get("tools") or ("all",)),
            )
            self._scopes[scope.scope_name] = scope

    def _save(self) -> None:
        if self._persist_path is None:
            return
        doc = {
            "entries": [
                {
                    "path": e.path,
                    "target_url": e.target_url,
                    "display_name": e.display_name,
                    "visibility": e.visibility,
                    "allowed_groups": list(e.allowed_groups),
                }
                for e in self._entries.values()
            ],
            "scopes": [
                {
                    "scope_name": s.scope_name,
                    "server": s.server,
                    "group": s.group,
                    "methods": list(s.methods),
                    "tools": list(s.tools),
                }
                for s in self._scopes.values()
            ],
        }
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._persist_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._persist_path)
