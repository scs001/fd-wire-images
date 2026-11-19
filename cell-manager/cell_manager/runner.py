"""cheap1 宿主 runner（add-self-serve-cell-provision 任务 2.4）：轮询 fd-wire
配给队列 → 本机执行 provision+grant → socat relay → PATCH registry 条目
proxy_pass_url → 回写结果。

编排契约（openspec add-self-serve-cell-provision / self-serve-onboarding
「自动配给链」Scenario）：

1. ``POST {FD_WIRE_INTERNAL_URL}/internal/cell-provision/next`` 领队头
   queued 任务（X-Internal-Token 闸）；无任务 204 → 本轮空转；
2. 幂等前置：provision 前 ``manager.status`` 查现态——已 ACTIVE 的客户
   跳过 provision/grant（重跑/重试安全；spec「已执行步骤幂等可重试」）；
3. ``provision`` → ``grant``（全部复用既有幂等与审计面——本 runner 不新增
   任何容器/registry 写路径）；
4. socat relay：**手工配方的代码化**（docs/paas-wire-line-install-gate-*
   §七 / 2026-10-24 handoff §三——仓内此前无 relay 代码，按 subprocess
   socat 模板实现：``socat TCP-LISTEN:<port>,fork,reuseaddr
   TCP:<container_name>:8000``，spawn 即返回；端口=基口+crc32(customer_id)
   稳定派生）。registry 条目的 proxy_pass_url 必须是集群可达的尾网地址，
   docker 网络名会被边缘渲染成注释块（生产实证）；
5. PATCH registry：复用既有 registry client 的 ``register``（服务端
   overwrite=true 即 PATCH 语义 + 显式 toggle 启用，触发边缘重渲染），
   target=relay 尾网地址；
6. ``POST .../{id}/result`` done + endpoint_url（=registry 入口+条目路径
   +/mcp，fd-wire 侧落 ``customer_cell_endpoints`` DB 兜底源）。

任一步异常 → result failed + error + step 痕（steps 逐级累积），运营邮件
由 fd-wire 侧回写 failed 时触发——回退现行手动链（首期无自动重试）。

依赖全注入（manager/grant/registry/client/relay/spawn 可替换）——测试用
Fake 驱动 + FakeClient 全链离线跑通，绝不触真网/真 docker。
"""

from __future__ import annotations

import json
import logging
import socket
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from .audit import AuditLog
from .config import CellManagerConfig
from .errors import CellManagerError, ConfigError
from .registry import ServerEntry, build_registry_client

logger = logging.getLogger(__name__)

#: 引擎容器内端口（与 CELL_TARGET_URL_TEMPLATE 缺省模板的 8000 同源）
_ENGINE_CONTAINER_PORT = 8000

#: 领任务/回写的 HTTP 超时（轮询面轻量请求；与 registry client 同量级）
_DEFAULT_HTTP_TIMEOUT = 15.0


@dataclass(frozen=True)
class ProvisionTask:
    """领到的配给任务（fd-wire 响应的最小投影）。"""

    id: int
    customer_id: str


class WireProvisionClient:
    """fd-wire /internal/cell-provision 面的 stdlib urllib 客户端。

    领任务 200 → ProvisionTask；204 → None（本轮空转）；401/5xx 等异常
    如实上抛（ runner 外层按轮询周期重试，绝不虚构任务）。
    """

    def __init__(self, base_url: str, token: str, *, timeout: float = _DEFAULT_HTTP_TIMEOUT) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"X-Internal-Token": self._token, "Accept": "application/json"}

    def claim_next(self) -> ProvisionTask | None:
        """领队头 queued 任务；无任务返回 None（204 如实语义）。"""
        url = f"{self._base}/internal/cell-provision/next"
        req = urllib.request.Request(url, data=b"", headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                status = resp.status
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise CellManagerError(
                f"fd-wire claim_next -> HTTP {exc.code}: {exc.read()[:200]!r}"
            ) from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            raise CellManagerError(f"fd-wire claim_next unreachable: {exc}") from exc
        if status == 204:
            return None
        body = json.loads(raw.decode("utf-8"))
        return ProvisionTask(id=int(body["id"]), customer_id=str(body["customer_id"]))

    def post_result(
        self,
        task_id: int,
        *,
        status: str,
        endpoint_url: str | None = None,
        error: str | None = None,
        steps: list[dict[str, Any]] | None = None,
    ) -> None:
        """回写执行结果（done: endpoint_url 必带 / failed: error 必带）。"""
        payload: dict[str, Any] = {"status": status}
        if endpoint_url is not None:
            payload["endpoint_url"] = endpoint_url
        if error is not None:
            payload["error"] = error
        if steps is not None:
            payload["steps"] = steps
        url = f"{self._base}/internal/cell-provision/{task_id}/result"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={**self._headers(), "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                if resp.status != 200:
                    raise CellManagerError(
                        f"fd-wire post_result -> unexpected HTTP {resp.status}"
                    )
        except urllib.error.HTTPError as exc:
            raise CellManagerError(
                f"fd-wire post_result -> HTTP {exc.code}: {exc.read()[:200]!r}"
            ) from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            raise CellManagerError(f"fd-wire post_result unreachable: {exc}") from exc


def socat_relay_command(listen_port: int, container_name: str) -> list[str]:
    """socat relay 配方 argv（手工开户配方 §三的代码化模板；可测纯函数）。"""
    return [
        "socat",
        f"TCP-LISTEN:{listen_port},fork,reuseaddr",
        f"TCP:{container_name}:{_ENGINE_CONTAINER_PORT}",
    ]


def spawn_relay(config: CellManagerConfig, customer_id: str, container_name: str) -> str:
    """spawn 一个 socat relay（幂等友好：reuseaddr+同口重复 spawn 旧进程
    抢占失败即退出，不叠加监听）；返回 registry proxy_pass_url 用的尾网地址。

    fail-closed：FD_WIRE_RELAY_HOST 未配置 → ConfigError（relay 地址是
    registry 条目的硬前置——配不出可达地址就不能注册）。
    """
    if not config.relay_host:
        raise ConfigError(
            "relay step requires FD_WIRE_RELAY_HOST to be set "
            "(registry proxy_pass_url must be cluster-reachable)"
        )
    port = config.relay_port(customer_id)
    subprocess.Popen(
        socat_relay_command(port, container_name),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return config.relay_target_url(customer_id)


def run_once(
    *,
    manager: Any,
    grant_face: Any,
    client: Any,
    config: CellManagerConfig,
    registry: Any = None,
    relay: Callable[[CellManagerConfig, str, str], str] = spawn_relay,
    audit: AuditLog | None = None,
) -> dict[str, Any]:
    """执行一轮：领任务 → 配给链 → 回写。返回本轮摘要（CLI JSON 出口）。

    manager/grant_face 为既有的 CellManager / CellGrant（Fake 驱动可注入）；
    client 为领任务/回写客户端（生产 WireProvisionClient / 测试假件）；
    registry 缺省按 config 重建（测试注入 FakeRegistryClient）。
    """
    task = client.claim_next()
    if task is None:
        return {"ok": True, "action": "runner", "task": None}
    steps: list[dict[str, Any]] = []
    try:
        customer_id = task.customer_id
        container_name = config.container_name(customer_id)
        # 幂等前置：现态 ACTIVE → 跳过 provision/grant（重跑/重试安全）
        current = manager.status(customer_id)
        if current.state == "active":
            steps.append({"step": "status", "detail": "already active, skip provision"})
        else:
            manager.provision(customer_id)
            steps.append({"step": "provision", "ok": True})
            grant_face.grant(customer_id)
            steps.append({"step": "grant", "ok": True})
        target = relay(config, customer_id, container_name)
        steps.append({"step": "relay", "target": target})
        # PATCH registry 条目 proxy_pass_url（register overwrite=true 即
        # PATCH 语义 + 显式 toggle 启用——边缘重渲染激活的正道）
        if registry is None:
            registry = build_registry_client(config)
        registry.register(
            ServerEntry(
                path=config.registry_path(customer_id),
                target_url=target,
                display_name=f"柏讯 cell · {customer_id}",
                visibility="group-restricted",
                allowed_groups=(config.cell_group(customer_id),),
            )
        )
        steps.append({"step": "registry", "target": target})
        endpoint_url = config.registered_endpoint_url(customer_id)
        client.post_result(
            task.id, status="done", endpoint_url=endpoint_url, steps=steps
        )
        _audit(audit, "runner-provision", customer_id, "done", steps)
        return {
            "ok": True,
            "action": "runner",
            "task": {"id": task.id, "customer_id": customer_id},
            "state": "done",
            "endpoint_url": endpoint_url,
        }
    except Exception as exc:  # noqa: BLE001 —— 任一步失败整体标 failed 留 step 痕
        error = f"{type(exc).__name__}: {exc}"
        steps.append({"step": "error", "error": error})
        try:
            client.post_result(task.id, status="failed", error=error, steps=steps)
        except Exception as post_exc:  # noqa: BLE001 —— 回写也失败：只留本地痕
            logger.error(
                "runner: result 回写失败（task=%s customer=%s）: %s",
                task.id,
                task.customer_id,
                post_exc,
            )
        _audit(audit, "runner-provision", task.customer_id, "failed", steps)
        return {
            "ok": False,
            "action": "runner",
            "task": {"id": task.id, "customer_id": task.customer_id},
            "state": "failed",
            "error": error,
            "steps": steps,
        }


def _audit(
    audit: AuditLog | None, action: str, customer_id: str, result: str,
    steps: list[dict[str, Any]],
) -> None:
    """runner 审计进 cell-manager audit 面（任务 2.5；audit 不可用时降级日志）。"""
    try:
        if audit is not None:
            audit.append(action=action, customer_id=customer_id, result=result, detail={"steps": steps})
            return
    except Exception:  # noqa: BLE001 —— 审计失败不阻断 runner 语义
        pass
    logger.info("runner %s %s: %s", action, customer_id, result)


def run_loop(
    *,
    manager: Any,
    grant_face: Any,
    client: Any,
    config: CellManagerConfig,
    registry: Any = None,
    relay: Callable[[CellManagerConfig, str, str], str] = spawn_relay,
    audit: AuditLog | None = None,
    stop: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> int:
    """常驻轮询（systemd timer 的常驻形态）：每 runner_poll_seconds 一轮。

    ``stop``/``sleep`` 可注入（测试以 stop 首轮即断 + sleep 记数离线跑）；
    单轮异常不退出（轮询面的韧性口径——下一周期自愈），返回处理轮数。
    """
    import time as _time

    sleeper = sleep or _time.sleep
    rounds = 0
    while stop is None or not stop():
        try:
            run_once(
                manager=manager,
                grant_face=grant_face,
                client=client,
                config=config,
                registry=registry,
                relay=relay,
                audit=audit,
            )
        except CellManagerError as exc:
            # 领任务失败（fd-wire 不可达/401）：轮询面不退出，下周期重试
            logger.warning("runner poll failed (will retry next interval): %s", exc)
        rounds += 1
        if stop is not None and stop():
            break
        sleeper(float(config.runner_poll_seconds))
    return rounds


__all__ = [
    "ProvisionTask",
    "WireProvisionClient",
    "run_loop",
    "run_once",
    "socat_relay_command",
    "spawn_relay",
]
