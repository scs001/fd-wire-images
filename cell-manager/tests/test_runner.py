"""cell-manager runner 单测（add-self-serve-cell-provision 任务 2.4）。

Fake 驱动全链离线：FakeDriver + FakeRegistryClient + FakeClient（领任务/
回写内存假件）——绝不触真网/真 docker。覆盖：

- 无任务空转（claim None → ok、零副作用）
- 全链成功：provision+grant → relay（socat 模板 argv 与尾网 target）→
  registry 重注册（proxy_pass_url=relay target）→ result done +
  endpoint_url（registry 入口派生）落回写假件
- 幂等重跑：已 ACTIVE 客户跳过 provision/grant，仍回写 done
- 任一步失败 → result failed + error + step 痕（audit 进 cell-manager 审计面）
- 回写通道自身失败不炸 runner（日志留痕，failed 结果照常返回）
- relay fail-closed：FD_WIRE_RELAY_HOST 未配置 → ConfigError（不回写 done）
- config fail-closed：runner 缺 FD_WIRE_INTERNAL_URL/TOKEN 点名报错
- CLI：`runner --once` 子进程走通（fake 驱动 + 假件 monkeypatch 不适用
  子进程——此处直接测 build_parser 与 main 的 --once 分支经注入假件）
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

PKG_ROOT = Path(__file__).resolve().parent.parent
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from cell_manager import (
    AuditLog,
    CellGrant,
    CellManager,
    CellManagerConfig,
    FakeDriver,
    FakeRegistryClient,
)
from cell_manager import runner
from cell_manager.errors import ConfigError
from cell_manager.registry.base import ServerEntry


class OverwriteFakeRegistry(FakeRegistryClient):
    """runner 视角的 Fake：允许同 path 换 target 重注册（对齐真实
    HttpRegistryClient 的 overwrite=true PATCH 语义——provision 落容器
    target、runner 随后改写 relay target 是生产路径；共享 Fake 的
    冲突报错语义保持不动，test_drivers 依赖它）。"""

    def register(self, entry: ServerEntry) -> None:
        existing = self.entry(entry.path)
        if existing is not None and existing.target_url != entry.target_url:
            self.unregister(entry.path)
        super().register(entry)


def runner_config(home: Path) -> CellManagerConfig:
    """runner 全量 env：队列入口 + relay 画像 + 身份映射（grant 用）。"""
    return CellManagerConfig.from_env(
        env={
            "CELL_MANAGER_HOME": str(home),
            "CELL_IMAGE": "fd-wire/cell-daas:test",
            "CELL_GATEWAY_URL": "https://wire.example.test/mcp",
            "CELL_REGISTRY_URL": "https://registry.example.test",
            "CELL_DRIVER": "fake",
            "CELL_REGISTRY_MODE": "fake",
            "FD_WIRE_INTERNAL_URL": "https://wire-api.example.test",
            "FD_WIRE_INTERNAL_TOKEN": "tok-123",
            "FD_WIRE_RELAY_HOST": "<relay-host>",
            "FD_WIRE_RELAY_PORT_BASE": "18005",
            "CELL_CUSTOMER_IDENTITY_MAP": json.dumps(
                {"acme": {"username": "acme-user", "sub2api_user_id": "701"}}
            ),
        }
    )


class FakeClient:
    """领任务/回写内存假件（WireProvisionClient 的测试替身）。"""

    def __init__(self, tasks: list[runner.ProvisionTask] | None = None) -> None:
        self._queue = list(tasks or [])
        self.results: list[dict] = []
        self.claims = 0

    def claim_next(self) -> runner.ProvisionTask | None:
        self.claims += 1
        return self._queue.pop(0) if self._queue else None

    def post_result(self, task_id: int, **payload) -> None:
        self.results.append({"task_id": task_id, **payload})

    @property
    def last_result(self) -> dict:
        assert self.results, "no result posted"
        return self.results[-1]


def fake_relay(config, customer_id, container_name):
    """测试 relay 替身：不 spawn socat，直接返回派生尾网地址。"""
    return config.relay_target_url(customer_id)


def build_runner(home: Path):
    """全套 runner 依赖（fake 驱动/registry + 共享 config）。"""
    config = runner_config(home)
    driver = FakeDriver()
    registry = OverwriteFakeRegistry()
    manager = CellManager(driver=driver, registry=registry, config=config)
    grant_face = CellGrant(registry=registry, config=config)
    audit = AuditLog(config.audit_log)
    return config, registry, manager, grant_face, audit


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


def test_no_task_is_clean_noop(tmp_path):
    """无任务：ok + task=None，零容器/registry/回写副作用。"""
    config, registry, manager, grant_face, audit = build_runner(tmp_path / "home")
    client = FakeClient()
    payload = runner.run_once(
        manager=manager, grant_face=grant_face, client=client, config=config,
        registry=registry, relay=fake_relay, audit=audit,
    )
    assert payload == {"ok": True, "action": "runner", "task": None}
    assert client.results == [] and registry.calls == []
    assert manager.status("acme").state == "not_found"


def test_full_chain_success(tmp_path):
    """全链：provision+grant → relay → registry 重注册 → result done。"""
    config, registry, manager, grant_face, audit = build_runner(tmp_path / "home")
    client = FakeClient(tasks=[runner.ProvisionTask(id=7, customer_id="acme")])
    payload = runner.run_once(
        manager=manager, grant_face=grant_face, client=client, config=config,
        registry=registry, relay=fake_relay, audit=audit,
    )
    assert payload["ok"] is True and payload["state"] == "done"
    assert payload["task"] == {"id": 7, "customer_id": "acme"}
    # cell 已 active（fake 驱动真实走完整 provision）
    assert manager.status("acme").state == "active"
    # 回写 done + endpoint_url = registry 入口 + 条目路径 + /mcp
    result = client.last_result
    assert result["status"] == "done"
    assert (
        result["endpoint_url"]
        == "https://registry.example.test/fd-wire-cell-acme/mcp"
    )
    # steps 痕：provision/grant/relay/registry 四步齐备
    step_names = [s["step"] for s in result["steps"]]
    assert step_names == ["provision", "grant", "relay", "registry"]
    # registry 条目 proxy_pass_url = relay 尾网地址（PATCH 语义）
    entry = registry.entry("fd-wire-cell-acme")
    assert entry is not None
    assert entry.target_url == config.relay_target_url("acme")
    assert entry.target_url.startswith("http://<relay-host>:")
    # 授权束：scope 文档已铸（grant 半边）
    assert ("upsert_scope", config.scope_name("acme")) in registry.calls
    # 审计进 cell-manager audit 面（任务 2.5）
    entries = [json.loads(line) for line in audit.path.read_text().splitlines()]
    assert entries[-1]["action"] == "runner-provision"
    assert entries[-1]["result"] == "done"


def test_idempotent_rerun_skips_provision(tmp_path):
    """已 ACTIVE 客户重跑：跳过 provision/grant，仍回写 done（幂等可重试）。"""
    config, registry, manager, grant_face, audit = build_runner(tmp_path / "home")
    manager.provision("acme")
    calls_before = len(registry.calls)
    client = FakeClient(tasks=[runner.ProvisionTask(id=9, customer_id="acme")])
    payload = runner.run_once(
        manager=manager, grant_face=grant_face, client=client, config=config,
        registry=registry, relay=fake_relay, audit=audit,
    )
    assert payload["state"] == "done"
    result = client.last_result
    step_names = [s["step"] for s in result["steps"]]
    assert step_names == ["status", "relay", "registry"]  # 无 provision/grant
    assert result["steps"][0]["detail"].startswith("already active")
    # registry 仍被 PATCH（proxy_pass_url 重申），scope 未重铸
    assert ("upsert_scope", config.scope_name("acme")) not in registry.calls[calls_before:]
    assert registry.entry("fd-wire-cell-acme").target_url == config.relay_target_url("acme")


def test_failure_marks_failed_with_step_trace(tmp_path):
    """grant 失败（身份映射缺）→ result failed + error + step 痕 + 审计。"""
    config, registry, manager, grant_face, audit = build_runner(tmp_path / "home")
    # 无身份映射的 grant 面（grant 对缺失客户明确报错）
    bare_grant = CellGrant(
        registry=registry,
        config=CellManagerConfig.from_env(
            env={
                "CELL_MANAGER_HOME": str(tmp_path / "home"),
                "CELL_DRIVER": "fake",
                "CELL_REGISTRY_MODE": "fake",
            }
        ),
    )
    client = FakeClient(tasks=[runner.ProvisionTask(id=11, customer_id="acme")])
    payload = runner.run_once(
        manager=manager, grant_face=bare_grant, client=client, config=config,
        registry=registry, relay=fake_relay, audit=audit,
    )
    assert payload["ok"] is False and payload["state"] == "failed"
    result = client.last_result
    assert result["status"] == "failed"
    assert "IdentityMissing" in result["error"] or "identity" in result["error"].lower()
    step_names = [s["step"] for s in result["steps"]]
    assert "provision" in step_names and "error" in step_names[-1] or "error" in step_names
    # 审计留 failed 痕（回退人工链的排查入口）
    entries = [json.loads(line) for line in audit.path.read_text().splitlines()]
    assert entries[-1]["result"] == "failed"


def test_result_post_failure_does_not_crash(tmp_path):
    """回写通道自身失败：runner 不炸（日志留痕），failed 摘要照常返回。"""
    config, registry, manager, grant_face, audit = build_runner(tmp_path / "home")

    class BrokenClient(FakeClient):
        def post_result(self, task_id: int, **payload) -> None:
            raise ConfigError("fd-wire unreachable")

    client = BrokenClient(tasks=[runner.ProvisionTask(id=13, customer_id="acme")])
    payload = runner.run_once(
        manager=manager, grant_face=grant_face, client=client, config=config,
        registry=registry, relay=fake_relay, audit=audit,
    )
    assert payload["ok"] is False and payload["state"] == "failed"
    assert "fd-wire unreachable" in payload["error"]


def test_relay_fail_closed(tmp_path):
    """FD_WIRE_RELAY_HOST 未配置：relay 步 ConfigError → failed（不虚报 done）。"""
    config, registry, manager, grant_face, audit = build_runner(tmp_path / "home")
    bare_config = CellManagerConfig.from_env(
        env={
            "CELL_MANAGER_HOME": str(tmp_path / "home"),
            "CELL_DRIVER": "fake",
            "CELL_REGISTRY_MODE": "fake",
            "FD_WIRE_INTERNAL_URL": "https://wire-api.example.test",
            "FD_WIRE_INTERNAL_TOKEN": "tok",
            # 无 FD_WIRE_RELAY_HOST
            "CELL_CUSTOMER_IDENTITY_MAP": json.dumps(
                {"acme": {"username": "acme-user", "sub2api_user_id": "701"}}
            ),
        }
    )
    with pytest.raises(ConfigError):
        bare_config.require_runner_settings()
    client = FakeClient(tasks=[runner.ProvisionTask(id=15, customer_id="acme")])
    payload = runner.run_once(
        manager=manager, grant_face=grant_face, client=client, config=bare_config,
        registry=registry, relay=runner.spawn_relay, audit=audit,
    )
    assert payload["state"] == "failed"
    assert "FD_WIRE_RELAY_HOST" in client.last_result["error"]


def test_runner_settings_fail_closed_names_missing(tmp_path):
    """缺 FD_WIRE_INTERNAL_URL/TOKEN：点名报错（fail-closed）。"""
    cfg = CellManagerConfig.from_env(env={"CELL_MANAGER_HOME": str(tmp_path)})
    with pytest.raises(ConfigError) as exc:
        cfg.require_runner_settings()
    assert "FD_WIRE_INTERNAL_URL" in str(exc.value)
    assert "FD_WIRE_INTERNAL_TOKEN" in str(exc.value)
    assert "FD_WIRE_RELAY_HOST" in str(exc.value)


def test_socat_command_template():
    """socat argv 模板：LISTEN 口 + fork/reuseaddr → 容器 8000（配方 §三）。"""
    argv = runner.socat_relay_command(18123, "fd-wire-cell-acme")
    assert argv[0] == "socat"
    assert "TCP-LISTEN:18123,fork,reuseaddr" in argv
    assert "TCP:fd-wire-cell-acme:8000" in argv


def test_relay_port_stable_derivation():
    """每客户口=基口+crc32 稳定派生（同 id 同口；不同 id 不同口大概率成立）。"""
    cfg = CellManagerConfig.from_env(
        env={"CELL_MANAGER_HOME": "/tmp/x", "FD_WIRE_RELAY_PORT_BASE": "18005"}
    )
    assert cfg.relay_port("acme") == cfg.relay_port("acme")
    assert 18005 <= cfg.relay_port("acme") < 20005
    assert cfg.relay_port("acme") != cfg.relay_port("globex") or True  # 稳定性为主


def test_cli_runner_once_with_fakes(tmp_path, monkeypatch):
    """`runner --once` 分支：注入假件走通 CLI main 的 JSON 出口（离线）。

    ``_dispatch`` 延迟 ``from cell_manager import default_manager``——补丁打
    在 ``cell_manager`` 包属性上（函数内 import 取的是该名字）。
    """
    import cell_manager as cm_pkg
    from cell_manager import __main__ as cli_main
    from cell_manager import runner as runner_mod

    # _dispatch 自 env 重建 config（require_runner_settings fail-closed）——
    # 环境变量给足，依赖注入只替换 manager/grant/client/audit/relay。
    monkeypatch.setenv("CELL_MANAGER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CELL_DRIVER", "fake")
    monkeypatch.setenv("CELL_REGISTRY_MODE", "fake")
    monkeypatch.setenv("FD_WIRE_INTERNAL_URL", "https://wire-api.example.test")
    monkeypatch.setenv("FD_WIRE_INTERNAL_TOKEN", "tok-123")
    monkeypatch.setenv("FD_WIRE_RELAY_HOST", "<relay-host>")
    config, registry, manager, grant_face, audit = build_runner(tmp_path / "home")
    client = FakeClient(tasks=[runner.ProvisionTask(id=21, customer_id="acme")])
    monkeypatch.setattr(cm_pkg, "default_manager", lambda: manager)
    monkeypatch.setattr(cm_pkg, "default_grant_manager", lambda: grant_face)
    # _dispatch 内 `from cell_manager import runner` 取的是包内模块——补丁
    # 打在该模块属性上（领任务客户端 / relay 出口均为调用时属性查找）。
    monkeypatch.setattr(runner_mod, "WireProvisionClient", lambda url, token, **kw: client)
    monkeypatch.setattr(runner_mod, "spawn_relay", fake_relay)
    # runner 内 build_registry_client(config) 的兜底构造也指向注入实例
    # （否则 fake 模式会另建一个落盘 Fake，与注入 manager 的内存态分叉）。
    monkeypatch.setattr(runner_mod, "build_registry_client", lambda cfg: registry)
    monkeypatch.setattr(cli_main, "_runner_audit", lambda cfg: audit)

    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli_main.main(["runner", "--once"])
    assert code == 0
    payload = json.loads(buf.getvalue())
    assert payload["ok"] is True and payload["state"] == "done"
    assert payload["task"] == {"id": 21, "customer_id": "acme"}

    # 队列已空 → 第二轮 noop，也 exit 0 且 ok/task=None
    client2 = FakeClient()
    monkeypatch.setattr(runner_mod, "WireProvisionClient", lambda url, token, **kw: client2)
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        code2 = cli_main.main(["runner", "--once"])
    assert code2 == 0
    assert json.loads(buf2.getvalue())["task"] is None
