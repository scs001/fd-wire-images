"""审计日志：JSONL 追加、时间/操作/客户/结果四要素、失败也留痕、purge 留档。"""

from __future__ import annotations

import json
import re

from cell_manager import UnknownCellError

_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")


def test_every_operation_appends_jsonl_entry(manager, config):
    manager.provision("acme")
    manager.pause("acme")
    manager.resume("acme")

    lines = (config.audit_log).read_text(encoding="utf-8").splitlines()
    entries = [json.loads(line) for line in lines]
    assert [(e["action"], e["result"]) for e in entries] == [
        ("provision", "ok"),
        ("pause", "ok"),
        ("resume", "ok"),
    ]
    for entry in entries:
        # 四要素齐备：时间 / 操作 / 客户 / 结果
        assert _TS_RE.match(entry["ts"]), entry
        assert entry["customer_id"] == "acme"
        assert entry["result"] == "ok"


def test_failed_operation_is_audited_with_error_result(manager, config):
    manager.provision("acme")
    try:
        manager.pause("ghost-corp")  # 未知客户 → 失败
    except UnknownCellError:
        pass

    entries = [json.loads(line) for line in config.audit_log.read_text(encoding="utf-8").splitlines()]
    assert entries[-1]["action"] == "pause"
    assert entries[-1]["customer_id"] == "ghost-corp"
    assert entries[-1]["result"] == "error"
    assert "no cell" in entries[-1]["detail"]["message"]


def test_purge_leaves_indelible_audit_record(manager, clock, config):
    manager.provision("acme")
    manager.deprovision("acme")
    clock.advance(days=31)
    manager.purge_expired()

    entries = [json.loads(line) for line in config.audit_log.read_text(encoding="utf-8").splitlines()]
    purge_entries = [e for e in entries if e["action"] == "purge"]
    assert len(purge_entries) == 1
    # 留档细节：删了什么、摘没摘条目、不可逆声明
    detail = purge_entries[0]["detail"]
    assert detail["irreversible"] is True
    assert detail["registry_unregistered"] is True
    assert detail["volume_removed"].endswith("/acme")


def test_audit_log_read_all_roundtrip(manager, config):
    manager.provision("acme")
    manager.status("acme")  # 查询不留审计（只记变更与失败）

    entries = manager._audit.read_all()
    assert [e["action"] for e in entries] == ["provision"]
