#!/usr/bin/env python3
"""行业看板批量导入器（wire-showcase-market 任务组 3）。

把 ``finddata/dashboards/`` 下的成品看板规格（完整 echarts option）转成 v2 的
``raw_option`` 面板，并上架为**平台自营发布方**名下的 market 档看板。

用法（在 fd-wire 根目录）::

    python -m fd_wire_api.scripts.import_showcase --dry-run   # 只报告
    python -m fd_wire_api.scripts.import_showcase            # 实跑

源文件形态（``*.spec.json``）::

    {"title": "...", "note": "...", "kpi": [...], "charts": [
        {"id": "chart-x", "title": "...", "source": "...", "height": 430,
         "option": { ...完整 echarts option... }}]}

幂等（design D4）：以**文件名归一化后的 slug** 为键，写进
``spec.spec.showcase_slug``。重复执行对同一看板产生新版本，而不是第二条看板。

上架全程走既有 service 链路（建 draft → 写 spec → publish → 设 market），
不直写表——这样状态机校验与 append-only 留痕都不会被绕过（design D5）。

安全：只写平台租户名下的产物。存在同 slug 的**非平台**产物时如实报冲突并跳过，
绝不修改客户的任何东西。
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

#: 源看板目录缺省值：优先「仓库根的 dashboards/」，退化到当前工作目录的
#: dashboards/。**不能**直接写 ``parents[4]``——容器里包被装在 /app 下，
#: 路径深度只有 parents[0..3]，索引 4 会 IndexError（生产实锤）。
def _default_dashboards_dir() -> Path:
    here = Path(__file__).resolve()
    # 仓库内布局：<repo>/api/fd_wire_api/scripts/x.py → parents[4] = <repo>
    if len(here.parents) > 4:
        candidate = here.parents[4] / "dashboards"
        if candidate.is_dir():
            return candidate
    # 容器内布局：/app/fd_wire_api/scripts/x.py → 用工作目录下的 dashboards/
    return Path.cwd() / "dashboards"


DEFAULT_DASHBOARDS_DIR = _default_dashboards_dir()

#: 面板固定跨度：成品看板本身是单列长页，迁入后每张图占满三列。
PANEL_SPAN = 3

#: 未声明高度时的缺省（与渲染器 DEFAULT_PANEL_HEIGHT 一致）
DEFAULT_HEIGHT = 220


@dataclass
class ImportReport:
    """逐条目结果（一次导入一份汇总，成功失败都留痕）。"""

    entries: list[dict[str, Any]] = field(default_factory=list)

    def add(self, **kwargs: Any) -> None:
        self.entries.append(kwargs)

    @property
    def succeeded(self) -> int:
        return sum(1 for e in self.entries if e["status"] in {"created", "updated"})

    @property
    def failed(self) -> int:
        return sum(1 for e in self.entries if e["status"] == "failed")

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": len(self.entries),
            "succeeded": self.succeeded,
            "failed": self.failed,
            "entries": self.entries,
        }


def slugify(name: str) -> str:
    """文件名 → slug：小写、非字母数字折叠为连字符、去首尾连字符。

    ``macro_cn_spec.json`` → ``macro-cn``（去掉 ``_spec`` 后缀与扩展名）。
    """
    stem = Path(name).name
    for suffix in (".spec.json", ".json"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    # 去掉尾部的 _spec / -spec（``macro_cn_spec.json`` → ``macro-cn``）——
    # 那是"这是规格文件"的标记，不是看板身份的一部分。
    stem = re.sub(r"[-_]spec$", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"[^0-9a-zA-Z]+", "-", stem).strip("-").lower()
    return stem


def load_source(path: Path) -> dict[str, Any]:
    """读源文件并做最小形状校验（缺 title / 缺 charts 即报错）。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("源文件顶层必须是 JSON 对象")
    if not isinstance(data.get("title"), str) or not data["title"].strip():
        raise ValueError("源文件缺少非空 title")
    charts = data.get("charts")
    if not isinstance(charts, list) or not charts:
        raise ValueError("源文件缺少非空 charts 数组")
    return data


def _kpi_to_panel(index: int, kpi: dict[str, Any]) -> dict[str, Any]:
    """源看板的 KPI 卡 → v2 kpi 面板（值存 rows[0][0]）。"""
    value = kpi.get("value")
    # 值可能是 "0.0%" 这类带单位的字符串：原样保留（渲染层 tabular-nums 呈现）
    panel: dict[str, Any] = {
        "id": f"kpi-{index + 1}",
        "type": "kpi",
        "title": str(kpi.get("label") or ""),
        "span": 1,
        "snapshot": {"columns": ["value"], "rows": [[value]]},
        "config": {},
    }
    # 源 KPI 的 change 文案作副标题（如「荣枯线下方」），cls 不迁移（纯样式）
    change = kpi.get("change")
    if isinstance(change, str) and change.strip():
        panel["config"]["subtitle"] = change
    return panel


def _chart_to_panel(index: int, chart: dict[str, Any]) -> dict[str, Any]:
    """源看板的一张图 → v2 面板（完整 echarts option 进 raw_option）。

    ``type`` 用 ``line``（基础档）——raw_option 面板的实际图形由配置决定，
    type 只影响「基础档还是高级档」的编辑器呈现与无 raw 时的回退。
    """
    option = chart.get("option")
    if not isinstance(option, dict):
        raise ValueError(f"图表 {chart.get('id')!r} 缺少 option 对象")
    height = chart.get("height")
    return {
        "id": f"chart-{index + 1}",
        "type": "line",
        "title": str(chart.get("title") or ""),
        "span": PANEL_SPAN,
        "source": str(chart.get("source") or ""),
        "height": (
            int(height)
            if isinstance(height, (int, float)) and height > 0
            else DEFAULT_HEIGHT
        ),
        # 直通：完整配置原样进逃生舱（v2 契约已实测可无损承载）
        "raw_option": copy.deepcopy(option),
    }


def build_showcase_spec(source: dict[str, Any], slug: str) -> dict[str, Any]:
    """源看板 → 产物信封（``{title, spec:{title, panels, showcase_slug}}``）。

    形状不合即抛 ``ValueError``（由 ``import_one`` 转成该条 failed，不产出
    半成品）。``load_source`` 已校验过一轮，这里再守一次是因为本函数也作为
    库函数被直接调用（缺 title 时 ``KeyError`` 是最难排查的那类错误）。
    """
    raw_title = source.get("title")
    if not isinstance(raw_title, str) or not raw_title.strip():
        raise ValueError("源看板缺少非空 title")
    charts = source.get("charts")
    if not isinstance(charts, list) or not charts:
        raise ValueError("源看板缺少非空 charts 数组")

    panels: list[dict[str, Any]] = []

    kpis = source.get("kpi")
    if isinstance(kpis, list):
        for index, kpi in enumerate(kpis):
            if isinstance(kpi, dict):
                panels.append(_kpi_to_panel(index, kpi))

    for index, chart in enumerate(charts):
        if isinstance(chart, dict):
            panels.append(_chart_to_panel(index, chart))

    title = raw_title.strip()[:200]
    body = {"title": title, "panels": panels, "showcase_slug": slug}
    note = source.get("note")
    if isinstance(note, str) and note.strip():
        body["note"] = note.strip()
    return {"title": title, "spec": body}


# ------------------------------------------------------------------ 导入执行


def _find_existing(db: Session, *, publisher: str, slug: str) -> Any:
    """按 slug 找既有的平台看板（幂等键在 spec 里，故需读版本行比对）。

    ``showcase_slug`` 是 spec 的自由扩展位，不在数据库列里——这里按
    平台租户 + kind 收窄后逐条比对（量级十几条，代价可忽略）。
    """
    from fd_wire_api.artifacts.models import Artifact, ArtifactVersion

    candidates = (
        db.execute(
            select(Artifact).where(
                Artifact.customer_id == publisher,
                Artifact.kind == "dashboard",
            )
        )
        .scalars()
        .all()
    )
    for artifact in candidates:
        latest = db.execute(
            select(ArtifactVersion)
            .where(ArtifactVersion.artifact_id == artifact.id)
            .order_by(ArtifactVersion.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        if latest is None:
            continue
        spec = latest.spec if isinstance(latest.spec, dict) else {}
        inner = spec.get("spec") if isinstance(spec.get("spec"), dict) else {}
        if inner.get("showcase_slug") == slug:
            return artifact
    return None


def _find_customer_conflict(db: Session, *, publisher: str, slug: str) -> Any:
    """查是否存在同 slug 的**非平台**产物（红线：绝不碰客户产物）。"""
    from fd_wire_api.artifacts.models import Artifact, ArtifactVersion

    candidates = (
        db.execute(
            select(Artifact).where(
                Artifact.customer_id != publisher,
                Artifact.kind == "dashboard",
            )
        )
        .scalars()
        .all()
    )
    for artifact in candidates:
        latest = db.execute(
            select(ArtifactVersion)
            .where(ArtifactVersion.artifact_id == artifact.id)
            .order_by(ArtifactVersion.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        if latest is None:
            continue
        spec = latest.spec if isinstance(latest.spec, dict) else {}
        inner = spec.get("spec") if isinstance(spec.get("spec"), dict) else {}
        if inner.get("showcase_slug") == slug:
            return artifact
    return None


def import_one(
    db: Session,
    *,
    path: Path,
    publisher: str,
    dry_run: bool,
) -> dict[str, Any]:
    """导入单份源看板；返回该条目的结果（异常不外抛，转 status=failed）。"""
    from fd_wire_api.artifacts import service as artifacts_service

    slug = slugify(path.name)
    try:
        source = load_source(path)
        envelope = build_showcase_spec(source, slug)
    except (ValueError, json.JSONDecodeError) as exc:
        return {
            "source": str(path),
            "slug": slug,
            "status": "failed",
            "error": f"源文件不可用：{exc}",
        }

    panel_count = len(envelope["spec"]["panels"])
    conflict = _find_customer_conflict(db, publisher=publisher, slug=slug)
    if conflict is not None:
        return {
            "source": str(path),
            "slug": slug,
            "status": "failed",
            "error": (
                f"slug 与客户产物冲突（artifact {conflict.id}，客户 "
                f"{conflict.customer_id}）——不修改客户数据，已跳过"
            ),
        }

    existing = _find_existing(db, publisher=publisher, slug=slug)

    if dry_run:
        return {
            "source": str(path),
            "slug": slug,
            "status": "would_create" if existing is None else "would_update",
            "title": envelope["title"],
            "panels": panel_count,
        }

    if existing is None:
        # 建板：走既有 service（draft → 写 spec → publish → market）
        artifact = artifacts_service.create_blank_artifact(
            db, customer_id=publisher, kind="dashboard", title=envelope["title"]
        )
        artifacts_service.update_spec(
            db, artifact=artifact, spec=envelope, customer_id=publisher
        )
        artifacts_service.publish_artifact(db, artifact, customer_id=publisher)
        artifact.visibility = "market"
        artifact.access_mode = "open"
        db.commit()
        # 重新读一次让版本号/状态反映本次写入
        db.refresh(artifact)
        status = "created"
    else:
        artifacts_service.update_spec(
            db, artifact=existing, spec=envelope, customer_id=publisher
        )
        if existing.status != "published":
            artifacts_service.publish_artifact(db, existing, customer_id=publisher)
        existing.visibility = "market"
        existing.access_mode = "open"
        db.commit()
        db.refresh(existing)
        artifact = existing
        status = "updated"

    return {
        "source": str(path),
        "slug": slug,
        "status": status,
        "artifact_id": artifact.id,
        "title": envelope["title"],
        "version": artifact.version,
        "panels": panel_count,
    }


def run_import(
    *,
    dashboards_dir: Path,
    dry_run: bool = False,
    only: list[str] | None = None,
) -> ImportReport:
    """批量导入入口：逐文件独立执行，单条失败不中断整批（design）。"""
    from fd_wire_api.admin.db import get_db as _unused  # noqa: F401 - 触发建库钩子
    from fd_wire_api.admin.db import get_session
    from fd_wire_api.ai.db import init_db as init_ai_db
    from fd_wire_api.market.platform_publisher import (
        PLATFORM_PUBLISHER_NAME,
        ensure_platform_publisher,
    )

    init_ai_db()

    report = ImportReport()
    # 两种命名并存：``*.spec.json``（intl-six-country）与 ``*_spec.json``
    # （macro_cn_spec）——同一份成品看板的不同年代命名习惯。
    files = sorted(
        set(dashboards_dir.glob("*.spec.json")) | set(dashboards_dir.glob("*_spec.json"))
    )
    if only:
        wanted = {slugify(name) for name in only}
        files = [f for f in files if slugify(f.name) in wanted]

    if not files:
        report.add(source=str(dashboards_dir), slug="-", status="failed",
                   error="未找到任何 *.spec.json 源文件")
        return report

    # 平台租户在平台库；产物在 ai 库——两个会话（与端点同口径）
    platform_db = get_session()
    ai_db = get_session()
    try:
        ensure_platform_publisher(platform_db)
        platform_db.commit()

        for path in files:
            result = import_one(
                ai_db,
                path=path,
                publisher=PLATFORM_PUBLISHER_NAME,
                dry_run=dry_run,
            )
            report.add(**result)
    finally:
        platform_db.close()
        ai_db.close()
    return report


# ------------------------------------------------------- HTTP 客户端模式
# （add-artifact-create-endpoint：建档→publish→visibility 全走开放端点，
#  解除对 kubectl exec 与 pod 内直连双库的依赖；pod 直跑（run_import）
#  保留为回退。仅依赖带内容创建 + slug 幂等 + 既有 publish/visibility 端点。）


def _http_request(
    base_url: str, token: str, method: str, path: str, payload: Any = None
) -> tuple[int, dict[str, Any]]:
    """一次带 Bearer 的 JSON 请求；非 2xx 抛 RuntimeError（detail 如实透传）。"""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw).get("detail", raw)
        except json.JSONDecodeError:
            detail = raw
        raise RuntimeError(f"{method} {path} → {exc.code}: {detail}") from exc


def http_import_one(
    base_url: str,
    token: str,
    *,
    path: Path,
    dry_run: bool,
) -> dict[str, Any]:
    """HTTP 模式导入单份源看板（异常不外抛，转 status=failed 由调用方兜）。

    POST /api/v1/artifacts（spec+slug 幂等：新建 201 / 重放 200）→ draft
    才 publish（published 重放跳过，避免 409）→ PUT visibility=market。
    幂等查重在平台租户域内（端点语义），pod 模式的「同 slug 客户产物
    冲突跳过」检查在 HTTP 面不可得——但幂等键不跨租户命中，客户产物
    依旧绝不被触碰。
    """
    slug = slugify(path.name)
    try:
        envelope = build_showcase_spec(load_source(path), slug)
    except (ValueError, json.JSONDecodeError) as exc:
        return {
            "source": str(path),
            "slug": slug,
            "status": "failed",
            "error": f"源文件不可用：{exc}",
        }

    panel_count = len(envelope["spec"]["panels"])
    if dry_run:
        return {
            "source": str(path),
            "slug": slug,
            "status": "would_create",
            "title": envelope["title"],
            "panels": panel_count,
        }

    status_code, body = _http_request(
        base_url,
        token,
        "POST",
        "/api/v1/artifacts",
        {"kind": "dashboard", "title": envelope["title"], "spec": envelope},
    )
    artifact_id = body["id"]
    if body.get("status") == "draft":
        _http_request(
            base_url, token, "POST", f"/api/v1/artifacts/{artifact_id}/publish"
        )
    _http_request(
        base_url,
        token,
        "PUT",
        f"/api/v1/artifacts/{artifact_id}/visibility",
        {"visibility": "market", "access_mode": "open"},
    )
    return {
        "source": str(path),
        "slug": slug,
        "status": "created" if status_code == 201 else "updated",
        "artifact_id": artifact_id,
        "title": envelope["title"],
        "version": body["version"],
        "panels": panel_count,
    }


def _collect_source_files(dashboards_dir: Path, only: list[str] | None) -> list[Path]:
    """源文件枚举（pod/HTTP 两模式同款）：两种历史命名 + --only slug 过滤。"""
    files = sorted(
        set(dashboards_dir.glob("*.spec.json")) | set(dashboards_dir.glob("*_spec.json"))
    )
    if only:
        wanted = {slugify(name) for name in only}
        files = [f for f in files if slugify(f.name) in wanted]
    return files


def run_import_http(
    *,
    dashboards_dir: Path,
    base_url: str,
    token: str,
    dry_run: bool = False,
    only: list[str] | None = None,
) -> ImportReport:
    """HTTP 模式批量导入入口：逐文件独立执行，单条失败不中断整批。"""
    report = ImportReport()
    files = _collect_source_files(dashboards_dir, only)
    if not files:
        report.add(source=str(dashboards_dir), slug="-", status="failed",
                   error="未找到任何 *.spec.json 源文件")
        return report
    for path in files:
        try:
            report.add(
                **http_import_one(base_url, token, path=path, dry_run=dry_run)
            )
        except (RuntimeError, OSError) as exc:
            report.add(
                source=str(path), slug=slugify(path.name), status="failed",
                error=str(exc),
            )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="行业看板批量导入（平台自营）")
    parser.add_argument(
        "--dashboards-dir",
        type=Path,
        default=DEFAULT_DASHBOARDS_DIR,
        help=f"源看板目录（缺省 {DEFAULT_DASHBOARDS_DIR}）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只报告不写入")
    parser.add_argument(
        "--only", nargs="*", help="只导入指定 slug/文件名（缺省全部）"
    )
    parser.add_argument(
        "--http-base-url",
        default=None,
        help="API 基址（如 https://wire.finddatatech.cloud）——给定时走 HTTP "
        "客户端模式（不再直连库，可在任意机器运行）；缺省 pod 内直跑",
    )
    parser.add_argument(
        "--http-token",
        default=os.environ.get("FD_WIRE_CONSOLE_TOKEN"),
        help="平台租户 console Bearer token（HTTP 模式必填；缺省读 "
        "FD_WIRE_CONSOLE_TOKEN 环境变量）",
    )
    args = parser.parse_args(argv)

    if args.http_base_url:
        if not args.http_token:
            parser.error("HTTP 模式需要 --http-token（或 FD_WIRE_CONSOLE_TOKEN）")
        report = run_import_http(
            dashboards_dir=args.dashboards_dir,
            base_url=args.http_base_url,
            token=args.http_token,
            dry_run=args.dry_run,
            only=args.only,
        )
    else:
        report = run_import(
            dashboards_dir=args.dashboards_dir, dry_run=args.dry_run, only=args.only
        )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    sys.exit(main())
