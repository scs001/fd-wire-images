"""FakeDriver：内存态容器驱动（测试 / 本机演示，无 docker 依赖）。

语义对齐 docker CLI 的直观行为：
- create 重名报错；create 不等于 start
- start/stop 幂等（对已处目标态的容器 no-op）
- remove 运行中容器须 force
- ``inspect`` 暴露 env 与 mounts——隔离测试据此断言「互不可见对方卷」

可选 ``persist_path``：容器表落盘（JSON）——CLI 演示模式下跨进程保持
Fake 容器存在感（簿记 cells.json 本来就落盘，容器表不同步会失真）。
单元测试缺省不传 → 纯内存。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from cell_manager.drivers.base import (
    CREATED,
    RUNNING,
    STOPPED,
    BindMount,
    ContainerInfo,
    ContainerSpec,
)
from cell_manager.errors import DriverError


class FakeDriver:
    """进程内假 docker：容器表 + 调用记录（测试断言用）。"""

    def __init__(self, persist_path: Path | str | None = None) -> None:
        self._persist_path = Path(persist_path) if persist_path is not None else None
        #: 全部调用轨迹 [(op, name), ...]——幂等/顺序断言用（不落盘，进程内视角）
        self.calls: list[tuple[str, str]] = []
        self._containers: dict[str, dict] = {}
        self._load()

    # ---- 协议实现 ----

    def create(self, spec: ContainerSpec) -> ContainerInfo:
        self.calls.append(("create", spec.name))
        self._require_absent(spec.name)
        self._containers[spec.name] = {
            "spec": spec,
            "status": CREATED,
        }
        self._save()
        return self.inspect(spec.name)

    def start(self, name: str) -> ContainerInfo:
        self.calls.append(("start", name))
        record = self._require(name)
        record["status"] = RUNNING
        self._save()
        return self.inspect(name)

    def stop(self, name: str) -> ContainerInfo:
        self.calls.append(("stop", name))
        record = self._require(name)
        if record["status"] == RUNNING:
            record["status"] = STOPPED
        self._save()
        return self.inspect(name)

    def wait_ready(self, name: str, *, timeout: float) -> None:
        """Fake 语义：容器存在即就绪（调用留痕供顺序断言）。"""
        self.calls.append(("wait_ready", name))
        self._require(name)

    def remove(self, name: str, force: bool = False) -> None:
        self.calls.append(("remove", name))
        record = self._require(name)
        if record["status"] == RUNNING and not force:
            raise DriverError(
                f"cannot remove running container {name!r} without force=True"
            )
        del self._containers[name]
        self._save()

    def exists(self, name: str) -> bool:
        return name in self._containers

    def inspect(self, name: str) -> ContainerInfo:
        record = self._require(name)
        spec: ContainerSpec = record["spec"]
        return ContainerInfo(
            name=spec.name,
            status=record["status"],
            image=spec.image,
            env=dict(spec.env),
            mounts=tuple(spec.binds),
        )

    # ---- 测试辅助 ----

    @property
    def names(self) -> list[str]:
        return sorted(self._containers)

    def call_count(self, op: str, name: str) -> int:
        return sum(1 for called_op, called_name in self.calls if called_op == op and called_name == name)

    # ---- 落盘（可选） ----

    def _load(self) -> None:
        if self._persist_path is None or not self._persist_path.exists():
            return
        doc = json.loads(self._persist_path.read_text(encoding="utf-8"))
        for name, rec in doc.items():
            spec = rec["spec"]
            self._containers[name] = {
                "spec": ContainerSpec(
                    name=spec["name"],
                    image=spec["image"],
                    env=dict(spec["env"]),
                    binds=tuple(
                        BindMount(host_path=b[0], container_path=b[1]) for b in spec["binds"]
                    ),
                ),
                "status": rec["status"],
            }

    def _save(self) -> None:
        if self._persist_path is None:
            return
        doc: dict[str, Any] = {
            name: {
                "spec": {
                    "name": rec["spec"].name,
                    "image": rec["spec"].image,
                    "env": dict(rec["spec"].env),
                    "binds": [[b.host_path, b.container_path] for b in rec["spec"].binds],
                },
                "status": rec["status"],
            }
            for name, rec in self._containers.items()
        }
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._persist_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._persist_path)

    # ---- 内部 ----

    def _require(self, name: str) -> dict:
        try:
            return self._containers[name]
        except KeyError:
            raise DriverError(f"no such container: {name!r}") from None

    def _require_absent(self, name: str) -> None:
        if name in self._containers:
            raise DriverError(f"container {name!r} already exists")
