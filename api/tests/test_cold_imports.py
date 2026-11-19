"""冷启动导入矩阵：每个模块单独起一个解释器，各自当一次"第一个被导入的"。

导入环只有**某个模块先被导入**时才炸：整个测试套件因为别处先导入了别的模块，一路
绿；线上也只是碰巧靠入口的导入顺序活着。2026-11-13 的
``datasets.ingest → ai → entities → data_plane → datasets.ingest`` 就是这么活了两周，
最后在线上 pod 里 ``import fd_wire_api.entities.db`` 直接 ImportError 才暴露。

所以这里**不 import**——那是假的，本文件自己跑在已加载的进程里。改成每次 fork 一个
干净解释器导入一个模块，报错文本原样带回断言消息。

为什么并行：43 个模块 × ~0.7s 串行要半分钟；线程池八个并发压到几秒，代价可接受，
覆盖面是全量（每个 ``*/db.py`` 与 ``*/routes.py`` 加应用入口），不是挑几个。
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

API_ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKAGE_ROOT = API_ROOT / "fd_wire_api"


def _targets() -> list[str]:
    """每个包的 ``db`` 与 ``routes`` 是"可能被第一个导入"的入口，加 app 与调度链。"""
    found: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        if path.name not in {"db.py", "routes.py"}:
            continue
        rel = path.relative_to(API_ROOT).with_suffix("")
        found.append(".".join(rel.parts).removesuffix(".__init__"))
    found += ["fd_wire_api.app", "fd_wire_api.materialization.runner"]
    return sorted(set(found))


def _cold_import(module: str) -> tuple[str, int, str]:
    env = dict(os.environ)
    # 继承父进程的解析路径（含 vendored 内核 wheel 可能被装到的位置）：
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    proc = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=str(API_ROOT),
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )
    return module, proc.returncode, (proc.stderr or "").strip()


def test_every_entry_module_imports_when_it_is_imported_first() -> None:
    targets = _targets()
    assert len(targets) >= 30, f"expected the whole entry matrix, saw {len(targets)}"

    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for module, code, stderr in pool.map(_cold_import, targets):
            if code != 0:
                tail = "\n".join(stderr.splitlines()[-6:])
                failures.append(f"{module}:\n{tail}")
    assert not failures, (
        "cold import failed — an import cycle came back (each module must be "
        "importable as the FIRST import in a fresh interpreter):\n\n"
        + "\n\n".join(failures)
    )


@pytest.mark.parametrize(
    "module",
    [
        "fd_wire_api.entities.db",
        "fd_wire_api.datasets.ingest",
        "fd_wire_api.data_plane.data_routes",
    ],
)
def test_the_two_known_cycles_stay_broken(module: str) -> None:
    """把 2026-11-13 实测断掉的两个环钉成回归：pedantic 版，慢一点也无所谓。"""
    name, code, stderr = _cold_import(module)
    assert code == 0, f"{name} must import cold:\n{stderr}"
