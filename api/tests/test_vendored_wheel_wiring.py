"""仓内 vendored wheel 的接线守卫。

**为什么需要它**（2026-11-13 实历）：`api/vendor/fd_ontology_kernel-0.1.0-py3-none-any.whl`
自 7c31f95 起就在仓里、也被 git 跟踪，但没有任何一处把它装进去——
`pyproject.toml` 没声明它、`Dockerfile` 既没 `COPY vendor` 也没 `--find-links`。
于是镜像里根本没有 `fd_ontology_kernel`；而 `entities/versioning.py` 在**模块顶层**
import 它，`app.py` 启动即 import `entities` ⇒ 容器 import 期就崩。
三重假绿同时成立：构建期 `pip install` 成功、本地单测全绿（手塞 PYTHONPATH）、
`kubectl get pods` 之外没人去 `exec` 里试 import。

**判据分两处，因为它们由两个解析器各自读**：
- uv 只认 `[tool.uv.sources]` / 依赖声明（`pyproject.toml`）；
- pip 只认 `--find-links` / 直接路径（`Dockerfile`）。

只改一处就等于只修了一半，而另一半不会报错——这正是本用例存在的理由。
"""

from __future__ import annotations

import pathlib
import re

API_DIR = pathlib.Path(__file__).resolve().parent.parent


def _normalize(name: str) -> str:
    """PEP 503 归一化：`fd_ontology_kernel` 与 `fd-ontology-kernel` 是同一个名字。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def wheel_dists(wheels: list[str]) -> list[str]:
    """wheel 文件名 → 归一化的发行名（`<name>-<version>-<tags>.whl`）。"""
    out = []
    for filename in wheels:
        stem = filename[: -len(".whl")] if filename.endswith(".whl") else filename
        out.append(_normalize(stem.split("-")[0]))
    return out


def undeclared_wheels(wheels: list[str], pyproject_text: str) -> list[str]:
    """pyproject 里没被声明的 vendored wheel（uv 侧缺口）。"""
    declared = _normalize(pyproject_text)
    return [w for w, dist in zip(wheels, wheel_dists(wheels)) if f'"{dist}' not in declared and f"'{dist}" not in declared]


def dockerfile_gaps(wheels: list[str], dockerfile_text: str) -> list[str]:
    """镜像构建拿不到 vendored wheel 的原因（pip 侧缺口）。

    要求同时满足：`vendor` 目录被 COPY 进构建上下文，且安装命令行让 pip
    能看见它（`--find-links vendor` 或直接点名 .whl）。
    """
    gaps: list[str] = []
    if not wheels:
        return gaps
    if not re.search(r"^\s*COPY\s+vendor\b", dockerfile_text, re.MULTILINE):
        gaps.append("Dockerfile 没有 `COPY vendor ./vendor`——wheel 进不了构建上下文")
    installs = re.findall(r"pip install[^\n]*(?:\\\n[^\n]*)*", dockerfile_text)
    joined = " ".join(installs)
    if "--find-links vendor" not in joined and ".whl" not in joined:
        gaps.append("安装命令行既没有 `--find-links vendor` 也没直接点名 .whl——pip 找不到它")
    return gaps


def test_every_vendored_wheel_is_declared_in_pyproject() -> None:
    wheels = sorted(p.name for p in (API_DIR / "vendor").glob("*.whl"))
    assert wheels, "vendor/ 下没有 wheel：若已改为 PyPI 发布，请删掉本用例并说明理由"
    pyproject = (API_DIR / "pyproject.toml").read_text(encoding="utf-8")
    missing = undeclared_wheels(wheels, pyproject)
    assert not missing, (
        f"vendored wheel 没有在 pyproject 里声明，uv/CI 都不会装：{missing}。"
        " 在 dependencies 里加条目，并在 [tool.uv.sources] 指向该文件。"
    )


def test_dockerfile_copies_and_installs_vendored_wheels() -> None:
    wheels = sorted(p.name for p in (API_DIR / "vendor").glob("*.whl"))
    dockerfile = (API_DIR / "Dockerfile").read_text(encoding="utf-8")
    gaps = dockerfile_gaps(wheels, dockerfile)
    assert not gaps, f"镜像里拿不到 vendored wheel：{gaps}"


def test_checkers_can_go_red() -> None:
    """自证：判据不是恒绿。去掉 pyproject 的声明 / 去掉 Dockerfile 的两处，各报一次。"""
    wheels = ["fd_ontology_kernel-0.1.0-py3-none-any.whl"]
    assert undeclared_wheels(wheels, 'dependencies = ["fd-ontology-kernel>=0.1.0"]') == []
    assert undeclared_wheels(wheels, 'dependencies = ["httpx>=0.27"]') == ["fd_ontology_kernel-0.1.0-py3-none-any.whl"]

    ok_docker = "COPY vendor ./vendor\nRUN pip install --find-links vendor '.[mongo]'\n"
    assert dockerfile_gaps(wheels, ok_docker) == []
    assert dockerfile_gaps(wheels, "RUN pip install --find-links vendor '.[mongo]'\n")
    assert dockerfile_gaps(wheels, "COPY vendor ./vendor\nRUN pip install '.[mongo]'\n")
