"""生命周期事件 → 平台溯源账本转发（wire-provenance-ledger 任务 4.2）。

cell-manager 的生命周期操作（provision/pause/resume/deprovision/
cancel_deprovision/purge）在 ``_run`` 落 JSONL 审计的同时，向 fd_wire_api
的 ``POST /api/v1/provenance/ingest`` 发 ``cell.lifecycle`` 事件。

可靠性：持久 outbox（``<home>/audit/provenance_outbox.jsonl``）——先落盘
再尝试发送，2xx 才出队；api 不可达时事件留在 outbox，下一次 emit 重试
（事件携带稳定 event_id，账本侧按其幂等去重，重发安全）。生命周期操作
本身 SHALL NOT 因转发失败而失败（转发是 best-effort，JSONL 审计仍是
兜底留档）。

env：

- ``CELL_PROVENANCE_URL``：ingest 端点全 URL；空 = 转发禁用（纯 JSONL）
- ``CELL_PROVENANCE_TOKEN``：``X-Provenance-Token`` 头（与 api 侧
  ``FD_WIRE_PROVENANCE_INGEST_TOKEN`` 同值）
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

_TIMEOUT_SECONDS = 3.0


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class ProvenanceForwarder:
    """outbox 模式的账本事件转发器（标准库实现，无第三方依赖）。"""

    def __init__(
        self,
        *,
        url: str,
        token: str,
        outbox_path: Path,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._url = url.rstrip("/")
        self._token = token
        self._outbox = outbox_path
        self._sleep = sleep  # 测试注入用；生产路径不 sleep

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        home: Path | None = None,
    ) -> "ProvenanceForwarder | None":
        """按 env 构造；``CELL_PROVENANCE_URL`` 为空返回 None（禁用）。"""
        e = dict(os.environ if env is None else env)
        url = (e.get("CELL_PROVENANCE_URL") or "").strip()
        if not url:
            return None
        root = home or Path(
            e.get("CELL_MANAGER_HOME") or Path.home() / ".fd-wire" / "cell-manager"
        )
        return cls(
            url=url,
            token=(e.get("CELL_PROVENANCE_TOKEN") or "").strip(),
            outbox_path=root / "audit" / "provenance_outbox.jsonl",
        )

    # ---- 对外 ----

    def emit(self, action: str, customer_id: str, detail: dict[str, Any] | None) -> None:
        """落 outbox 并尝试清空（成功 op 后调用；失败不抛）。"""
        event = {
            "event_id": f"evt_{uuid.uuid4().hex}",
            "ledger": str(customer_id),
            "type": "cell.lifecycle",
            "actor": {"kind": "platform", "id": "cell-manager"},
            "subject": {"kind": "cell", "id": str(customer_id)},
            "payload": {"action": action, "detail": _slim(detail)},
        }
        self._outbox.parent.mkdir(parents=True, exist_ok=True)
        with self._outbox.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        try:
            self.flush()
        except Exception:  # noqa: BLE001 —— 转发 best-effort，绝不打断生命周期操作
            pass

    def flush(self) -> int:
        """尝试把 outbox 全部发出去；返回成功条数。失败行保留待重试。"""
        if not self._outbox.exists():
            return 0
        lines = [ln for ln in self._outbox.read_text(encoding="utf-8").splitlines() if ln.strip()]
        sent = 0
        remaining: list[str] = []
        for line in lines:
            event = json.loads(line)
            body = dict(event)
            body["ts_platform"] = _utc_now_iso()
            req = urllib.request.Request(  # noqa: S310 —— 目标 URL 来自部署 env
                f"{self._url}",
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    **({"X-Provenance-Token": self._token} if self._token else {}),
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
                    if 200 <= resp.status < 300:
                        sent += 1
                    else:
                        remaining.append(line)
            except (urllib.error.URLError, OSError, TimeoutError):
                remaining.append(line)
        # 原子重写剩余（崩窗最多重发，账本按 event_id 幂等）
        tmp = self._outbox.with_suffix(".jsonl.tmp")
        tmp.write_text("".join(ln + "\n" for ln in remaining), encoding="utf-8")
        tmp.replace(self._outbox)
        return sent

    def pending(self) -> int:
        """outbox 待发条数（可观测/测试用）。"""
        if not self._outbox.exists():
            return 0
        return sum(1 for ln in self._outbox.read_text(encoding="utf-8").splitlines() if ln.strip())


def _slim(detail: dict[str, Any] | None) -> dict[str, Any]:
    """detail 瘦身：只保留可安全入账的标量/浅层字段（卷路径等照样留，不含凭据）。"""
    if not detail:
        return {}
    out: dict[str, Any] = {}
    for key, value in detail.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
        else:
            out[key] = str(value)
    return out
