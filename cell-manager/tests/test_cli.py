"""CLI（python -m cell_manager）：子进程端到端，Fake 驱动走通全生命周期。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent


def run_cli(*args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CELL_")  # 隔离宿主 CELL_* 配置
    }
    env.update(
        {
            "CELL_DRIVER": "fake",
            "CELL_REGISTRY_MODE": "fake",
            "PYTHONPATH": str(PKG_ROOT),
        }
    )
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-m", "cell_manager", *args],
        capture_output=True,
        text=True,
        cwd=str(PKG_ROOT),
        env=env,
        timeout=60,
    )


def _payload(proc: subprocess.CompletedProcess) -> dict:
    return json.loads(proc.stdout)


def test_cli_full_lifecycle_on_fake_driver(tmp_path):
    home = tmp_path / "demo-home"
    env = {
        "CELL_MANAGER_HOME": str(home),
        "CELL_IMAGE": "fd-wire/cell-daas:test",
        "CELL_RETENTION_DAYS": "30",
    }

    # provision → active
    proc = run_cli("provision", "acme", env_extra=env)
    assert proc.returncode == 0, proc.stderr
    assert _payload(proc)["state"] == "active"

    # 幂等 provision
    proc = run_cli("provision", "acme", env_extra=env)
    assert proc.returncode == 0
    assert _payload(proc)["state"] == "active"

    # pause → 卷保留
    proc = run_cli("pause", "acme", env_extra=env)
    assert _payload(proc)["state"] == "paused"
    assert (home / "volumes" / "acme" / "daas.db").is_file()

    # resume
    proc = run_cli("resume", "acme", env_extra=env)
    assert _payload(proc)["state"] == "active"

    # deprovision → retention 30 天
    proc = run_cli("deprovision", "acme", env_extra=env)
    payload = _payload(proc)
    assert payload["state"] == "retention"
    assert payload["days_remaining"] == 30

    # 保留期内反悔 → active
    proc = run_cli("cancel-deprovision", "acme", env_extra=env)
    assert _payload(proc)["state"] == "active"

    # 再注销（0 天保留 → 立即到期）并清除
    run_cli("deprovision", "acme", env_extra={**env, "CELL_RETENTION_DAYS": "0"})
    proc = run_cli("purge-expired", env_extra=env)
    assert proc.returncode == 0
    assert [cell["customer_id"] for cell in _payload(proc)["purged"]] == ["acme"]
    # status → not_found（audit 里有 purge 留档）
    proc = run_cli("status", "acme", env_extra=env)
    assert _payload(proc)["state"] == "not_found"
    audit_lines = (home / "audit" / "audit.log").read_text().splitlines()
    assert any(json.loads(line)["action"] == "purge" for line in audit_lines)


def test_cli_unknown_customer_status_and_error_exit(tmp_path):
    env = {"CELL_MANAGER_HOME": str(tmp_path / "home2")}

    proc = run_cli("status", "ghost-corp", env_extra=env)
    assert proc.returncode == 0
    assert _payload(proc)["state"] == "not_found"

    proc = run_cli("pause", "ghost-corp", env_extra=env)
    assert proc.returncode == 1
    payload = _payload(proc)
    assert payload["ok"] is False
    assert payload["error"]["type"] == "UnknownCellError"

    # 审计连失败也留痕
    audit_lines = (tmp_path / "home2" / "audit" / "audit.log").read_text().splitlines()
    last = json.loads(audit_lines[-1])
    assert (last["action"], last["result"]) == ("pause", "error")


def test_cli_invalid_customer_id_rejected(tmp_path):
    proc = run_cli("provision", "../escape", env_extra={"CELL_MANAGER_HOME": str(tmp_path)})
    assert proc.returncode == 1
    assert _payload(proc)["error"]["type"] == "InvalidCustomerIdError"


def test_cli_missing_customer_id_fails(tmp_path):
    proc = run_cli("provision", env_extra={"CELL_MANAGER_HOME": str(tmp_path)})
    assert proc.returncode != 0
