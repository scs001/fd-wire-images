"""质量快照服务（wire-asset-catalog 任务组 4，design D2/D4/D6）。

职责与红线：

- **计算全部在引擎侧**（红线 2 的落地）：平台侧只经既有引擎调用通道
  （:class:`~fd_wire_api.datasets.ingest.EngineToolClient`）调
  ``customer_dataset_quality`` 工具，取回聚合计数与比率落快照表；
  本模块（乃至整个 quality 切片）不存在任何读取 cell 数据内容的路径。
- **payload 白名单过滤**：引擎输出映射到快照列时逐字段校验类型，缺字段
  /类型不对诚实置 NULL，绝不猜数、绝不透传引擎多给的字段（防样本行
  混入——快照 SHALL NOT 包含任何数据值）。
- **触发与失败隔离**（spec「快照触发时机」）：上传建档数据落引擎后
  （create）、连接器同步 commit 成功后（sync）、派生重算成功后
  （materialize）由 :func:`notify_dataset_changed` 自动触发——该入口
  **绝不抛异常**（一次快照失败只落一条 ``failed`` 快照行，数据集本身的
  同步/派生结果不受影响）；手动端点直调 :func:`capture_snapshot`。
- **引擎版本滚动窗口降级**（design D6）：cell 未开通（EngineUnavailable）
  或旧镜像无该工具（工具缺失类报错）→ ``unsupported`` 快照行；自动触发
  带 24h 退避（上一条 ``unsupported`` 落在退避窗口内则静默跳过，防把旧
  引擎 cell 打爆），手动触发不受退避限制。
- **新鲜度读取时计算不落库**（design D4）：判定与展示分离——存原始
  时间戳，读时按 ``cadence_hint × 容许倍数`` 算 过期/正常/未知；节奏
  未知或无刷新信息时恒「未知」，绝不误判「过期」。

``cadence_hint`` 读 ``dataset_asset_meta``（姊妹切片 fd_wire_api/assets，
本切片只读、绝不写）：懒导入其模型，模块缺席/表未建时回落原生 SQL 只读
查询（仍取不到 → 视为未声明节奏）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..datasets.db import get_session
from ..datasets.ingest import EngineCallError, EngineUnavailable
from ..datasets.models import Dataset, DatasetSync
from .models import (
    STATUS_FAILED,
    STATUS_OK,
    STATUS_UNSUPPORTED,
    DatasetQualitySnapshot,
)
from .settings import get_settings

logger = logging.getLogger(__name__)

#: 引擎质量工具全名（fd-daas-mcp customer_dataset 组注册名；平台侧零计算）
TOOL_QUALITY = "customer_dataset_quality"

#: 快照触发方式常量（models.TRIGGERED_BY_VALUES 的代码侧引用）
TRIGGER_CREATE = "create"
TRIGGER_SYNC = "sync"
TRIGGER_MATERIALIZE = "materialize"
TRIGGER_MANUAL = "manual"

#: 声明节奏 → 容许周期（小时）；未知节奏值不参与判定（恒「未知」）
CADENCE_PERIOD_HOURS: dict[str, float] = {"daily": 24.0, "weekly": 168.0, "monthly": 720.0}

#: 呈现口径标注（spec「呈现如实」：任何质量信息界面都带）
DISCLAIMER = "指示性检查，非质量认证"

#: 旧 cell 引擎（未滚动到含质量工具的镜像）的工具缺失错误特征——MCP
#: 服务端对未注册工具的报错文案，大小写不敏感子串匹配（照 entities
#: link 工具同款先例；EngineUnavailable 单独短路，不走本表）。
_UNSUPPORTED_MARKERS: tuple[str, ...] = (
    "unknown tool",
    "tool not found",
    "no such tool",
    "not a valid tool",
    "invalid tool",
    "未知工具",
    "工具不存在",
    # 引擎侧内容代检查执行体缺失（wire-quality-rules-v2：旧 cell 镜像
    # 无 pandera → 引擎返回此文案；平台侧同归 unsupported 降级语义）
    "引擎暂不支持该检查类型",
    "内容代检查执行体未安装",
)

#: 引擎客户端工厂覆写（测试/调度链注入假件；None=用真实 EngineToolClient）
_engine_client_factory: Callable[[str], Any] | None = None


def set_engine_client_factory(factory: Callable[[str], Any] | None) -> None:
    """注入/清除引擎客户端工厂（测试用；生产路径不碰）。"""
    global _engine_client_factory
    _engine_client_factory = factory


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _is_unsupported_error(exc: Exception) -> bool:
    """工具缺失类错误特征匹配（EngineUnavailable 由调用方单独短路）。"""
    text = str(exc).lower()
    return any(marker in text for marker in _UNSUPPORTED_MARKERS)


# ------------------------------------------------------------- 引擎输出净化


def _sanitize_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _sanitize_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _sanitize_null_rates(value: Any) -> list[dict] | None:
    """空值率数组白名单过滤：只保留 {column, null_rate, sampled_rows}。

    逐项校验类型（列名非空字符串、比率数值、抽样行数整数；不合格子项
    置 NULL 不丢弃行）；**引擎多给的任何其他字段（含可能的样本行/数据
    值）一律不透传**——快照 SHALL NOT 包含任何数据值。
    """
    if not isinstance(value, list):
        return None
    out: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        column = item.get("column")
        if not isinstance(column, str) or not column.strip():
            continue
        out.append(
            {
                "column": column,
                "null_rate": _sanitize_float(item.get("null_rate")),
                "sampled_rows": _sanitize_int(item.get("sampled_rows")),
            }
        )
    return out


def _parse_engine_time(value: Any) -> datetime | None:
    """引擎侧计算时间（ISO 字符串）→ naive UTC datetime；解析失败 None。"""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def snapshot_to_dict(row: DatasetQualitySnapshot) -> dict:
    """快照行 → API dict（datetime 转 ISO；null_rates_json 展开）。"""
    return {
        "id": row.id,
        "dataset_id": row.dataset_id,
        "computed_at": row.computed_at.isoformat() if row.computed_at else None,
        "triggered_by": row.triggered_by,
        "status": row.status,
        "row_count": row.row_count,
        "pk_unique_ratio": row.pk_unique_ratio,
        "pk_declared": row.pk_declared,
        "null_rates": row.null_rates_json,
        "sample_limit_hit": row.sample_limit_hit,
        "error_text": row.error_text,
    }


# ------------------------------------------------------------- 快照读写


def latest_snapshot(db: Session, dataset_id: str) -> DatasetQualitySnapshot | None:
    """最新一条快照（按插入序；无快照返回 None）。"""
    return (
        db.execute(
            select(DatasetQualitySnapshot)
            .where(DatasetQualitySnapshot.dataset_id == dataset_id)
            .order_by(DatasetQualitySnapshot.id.desc())
            .limit(1)
        )
        .scalar_one_or_none()
    )


def snapshots_for(
    db: Session, dataset_id: str, *, limit: int | None = None
) -> list[DatasetQualitySnapshot]:
    """快照历史（新→旧；limit 缺省=保留期条数）。"""
    keep = limit if limit is not None else get_settings().quality_history_retention
    return list(
        db.execute(
            select(DatasetQualitySnapshot)
            .where(DatasetQualitySnapshot.dataset_id == dataset_id)
            .order_by(DatasetQualitySnapshot.id.desc())
            .limit(max(keep, 0))
        )
        .scalars()
        .all()
    )


def _prune_history(db: Session, dataset_id: str) -> None:
    """保留期裁剪：每数据集只留最近 N 条（append-only 下的留存策略）。"""
    keep = max(get_settings().quality_history_retention, 0)
    keep_ids = (
        select(DatasetQualitySnapshot.id)
        .where(DatasetQualitySnapshot.dataset_id == dataset_id)
        .order_by(DatasetQualitySnapshot.id.desc())
        .limit(keep)
    )
    db.execute(
        delete(DatasetQualitySnapshot).where(
            DatasetQualitySnapshot.dataset_id == dataset_id,
            DatasetQualitySnapshot.id.not_in(keep_ids),
        )
    )


def _resolve_engine_client(customer_id: str, client: Any = None) -> Any:
    """引擎客户端解析：调用方自带（钩子复用主流程句柄）→ 测试覆写工厂 → 真身。"""
    if client is not None:
        return client
    if _engine_client_factory is not None:
        return _engine_client_factory(customer_id)
    from ..datasets.ingest import EngineToolClient  # noqa: PLC0415 - 惰性防环

    return EngineToolClient(customer_id)


def _record(
    db: Session,
    *,
    dataset_id: str,
    triggered_by: str,
    status: str,
    computed_at: datetime,
    row_count: int | None = None,
    pk_unique_ratio: float | None = None,
    pk_declared: bool = False,
    null_rates: list[dict] | None = None,
    sample_limit_hit: bool = False,
    error_text: str | None = None,
) -> DatasetQualitySnapshot:
    """落一条快照行并裁剪保留期（唯一写入口——INSERT-only）。"""
    snapshot = DatasetQualitySnapshot(
        dataset_id=dataset_id,
        computed_at=computed_at,
        triggered_by=triggered_by,
        status=status,
        row_count=row_count,
        pk_unique_ratio=pk_unique_ratio,
        pk_declared=pk_declared,
        null_rates_json=null_rates,
        sample_limit_hit=sample_limit_hit,
        error_text=error_text,
    )
    db.add(snapshot)
    db.commit()
    _prune_history(db, dataset_id)
    db.commit()
    return snapshot


def capture_snapshot(
    customer_id: str,
    dataset: Dataset,
    triggered_by: str,
    *,
    client: Any = None,
    db: Session | None = None,
) -> DatasetQualitySnapshot:
    """抓一次质量快照：调引擎质量工具 → 净化映射 → 落快照行。

    错误分类（design D6 / spec「引擎不支持时降级显示」）：

    - cell 未开通 / 平台凭据未配置（``EngineUnavailable``）→ ``unsupported``；
    - 工具缺失类报错（旧镜像未滚动）→ ``unsupported``；
    - 其他一切错误（网络/工具报错/返回形态异常）→ ``failed`` + ``error_text``；
    - 引擎正常返回但缺字段 → ``ok`` + 该字段诚实 NULL（绝不猜数、绝不异常）。

    ``db`` 缺省自开会话（钩子路径）；请求路径（手动端点）传请求会话。
    本函数**会 commit**；失败隔离入口见 :func:`notify_dataset_changed`。
    """
    own_session = db is None
    session = db or get_session()
    try:
        engine = _resolve_engine_client(customer_id, client)
        now = _utcnow_naive()
        try:
            payload = engine.call(TOOL_QUALITY, {"dataset_key": dataset.key})
        except EngineUnavailable as exc:
            return _record(
                session,
                dataset_id=dataset.id,
                triggered_by=triggered_by,
                status=STATUS_UNSUPPORTED,
                computed_at=now,
                error_text=f"引擎暂不支持质量快照: {exc}",
            )
        except EngineCallError as exc:
            status = (
                STATUS_UNSUPPORTED if _is_unsupported_error(exc) else STATUS_FAILED
            )
            return _record(
                session,
                dataset_id=dataset.id,
                triggered_by=triggered_by,
                status=status,
                computed_at=now,
                error_text=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - 快照失败不外抛给主流程
            return _record(
                session,
                dataset_id=dataset.id,
                triggered_by=triggered_by,
                status=STATUS_FAILED,
                computed_at=now,
                error_text=f"{type(exc).__name__}: {exc}",
            )

        if not isinstance(payload, dict):
            return _record(
                session,
                dataset_id=dataset.id,
                triggered_by=triggered_by,
                status=STATUS_FAILED,
                computed_at=now,
                error_text=f"引擎返回形态异常（非 dict）: {type(payload).__name__}",
            )
        if payload.get("error"):
            return _record(
                session,
                dataset_id=dataset.id,
                triggered_by=triggered_by,
                status=STATUS_FAILED,
                computed_at=now,
                error_text=f"引擎拒绝: {payload['error']}",
            )

        pk_declared = payload.get("pk_declared") is True
        pk_unique_ratio: float | None = None
        if pk_declared:
            uniqueness = payload.get("pk_uniqueness")
            if isinstance(uniqueness, dict):
                # 未声明键列（pk_declared=False）时比例恒 None——不给伪造数字
                pk_unique_ratio = _sanitize_float(uniqueness.get("unique_ratio"))
        return _record(
            session,
            dataset_id=dataset.id,
            triggered_by=triggered_by,
            status=STATUS_OK,
            computed_at=_parse_engine_time(payload.get("computed_at")) or now,
            row_count=_sanitize_int(payload.get("row_count")),
            pk_unique_ratio=pk_unique_ratio,
            pk_declared=pk_declared,
            null_rates=_sanitize_null_rates(payload.get("null_rates")),
            sample_limit_hit=payload.get("sample_limit_hit") is True,
        )
    finally:
        if own_session:
            session.close()


def _should_skip_automatic(db: Session, dataset_id: str) -> bool:
    """``unsupported`` 退避（design D6）：最新一条 unsupported 落在退避窗口
    内 → 自动触发跳过（至多每日一次），手动触发不受限。"""
    latest = latest_snapshot(db, dataset_id)
    if latest is None or latest.status != STATUS_UNSUPPORTED:
        return False
    if latest.computed_at is None:
        return False
    backoff = timedelta(hours=get_settings().quality_unsupported_backoff_hours)
    return (_utcnow_naive() - latest.computed_at) < backoff


def notify_dataset_changed(
    dataset: Dataset, triggered_by: str, *, client: Any = None
) -> None:
    """自动触发钩子入口（失败隔离，**绝不抛异常**）。

    - api 层数据集（免费档轻量行存储）静默跳过：质量计算在 cell 引擎侧，
      平台侧直读被红线禁止，引擎侧也无其数据表——不落伪造快照；
    - ``unsupported`` 退避窗口内跳过（见 :func:`_should_skip_automatic`）；
    - 任何异常（含快照落库失败）只记日志——主流程（同步/派生/建档）的
      结果已在调用前落定，绝不因快照中断或回滚；
    - 快照后顺跑质量规则自动求值（wire-quality-rules）：独立失败隔离
      （规则求值的任何异常同样只记日志，绝不影响主流程）。
    """
    try:
        if getattr(dataset, "storage_tier", "cell") == "api":
            return
        db = get_session()
        try:
            if triggered_by != TRIGGER_MANUAL and _should_skip_automatic(
                db, dataset.id
            ):
                return
            capture_snapshot(
                dataset.customer_id, dataset, triggered_by, client=client, db=db
            )
        finally:
            db.close()
    except Exception:  # noqa: BLE001 - 快照失败不影响主流程（spec 明文）
        logger.exception(
            "质量快照自动触发失败（已隔离，主流程不受影响）dataset_id=%s trigger=%s",
            getattr(dataset, "id", "?"),
            triggered_by,
        )
    finally:
        from . import rules as _rules  # noqa: PLC0415 - 惰性防环

        _rules.auto_evaluate_rules(
            dataset=dataset, triggered_by=triggered_by, client=client
        )


# ------------------------------------------------------------- 新鲜度（读时计算）


def _cadence_hint(db: Session, dataset_id: str) -> str | None:
    """读资产元数据的期望节奏（**只读**，绝不写姊妹切片的表）。

    优先懒导入 ``fd_wire_api.assets.models.AssetMeta``（模块缺席/表未建时
    回落原生 SQL 只读查询；仍取不到 → None=未声明节奏）。
    """
    meta_model = None
    try:
        from ..assets.models import AssetMeta as meta_model  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - 姊妹模块未落地：回落原生 SQL
        meta_model = None
    try:
        if meta_model is not None:
            row = db.get(meta_model, dataset_id)
            cadence = getattr(row, "cadence_hint", None)
        else:
            from sqlalchemy import text  # noqa: PLC0415

            cadence = db.execute(
                text(
                    "SELECT cadence_hint FROM dataset_asset_meta"
                    " WHERE dataset_id = :dataset_id"
                ),
                {"dataset_id": dataset_id},
            ).scalar_one_or_none()
    except Exception:  # noqa: BLE001 - 表未建/查询失败 → 未声明（恒「未知」）
        return None
    return cadence if isinstance(cadence, str) and cadence.strip() else None


def _last_refresh_at(db: Session, dataset: Dataset) -> datetime | None:
    """最近成功刷新时间（新鲜度的原始时间源，读取时取、不落库）。

    源优先级（诚实递补，绝不编造）：

    1. 最新 ``ok`` 质量快照的 ``computed_at``；
    2. 最新 ``ok`` 同步行的 ``finished_at``（快照通道未铺开时的回退源）；
    3. 数据集自身 ``updated_at``（仅当 ``sync_status=ok``——上传建档即
       ``ok``，其余状态（syncing/failed/…）不代表一次成功刷新，返回 None）。
    """
    snap = (
        db.execute(
            select(DatasetQualitySnapshot)
            .where(
                DatasetQualitySnapshot.dataset_id == dataset.id,
                DatasetQualitySnapshot.status == STATUS_OK,
            )
            .order_by(DatasetQualitySnapshot.id.desc())
            .limit(1)
        )
        .scalar_one_or_none()
    )
    if snap is not None and snap.computed_at is not None:
        return snap.computed_at
    sync = (
        db.execute(
            select(DatasetSync)
            .where(
                DatasetSync.dataset_id == dataset.id,
                DatasetSync.status == "ok",
            )
            .order_by(DatasetSync.finished_at.desc())
            .limit(1)
        )
        .scalar_one_or_none()
    )
    if sync is not None and sync.finished_at is not None:
        return sync.finished_at
    if dataset.sync_status == "ok":
        return dataset.updated_at
    return None


def _freshness(
    cadence: str | None, last_refresh: datetime | None, *, now: datetime
) -> str:
    """判定规则（design D4）：节奏未知或无刷新信息恒「未知」（绝不「过期」）。"""
    period = CADENCE_PERIOD_HOURS.get(cadence or "")
    if period is None or last_refresh is None:
        return "unknown"
    allowed = timedelta(hours=period * get_settings().quality_stale_multiplier)
    return "stale" if (now - last_refresh) > allowed else "fresh"


def freshness_statuses(customer_id: str, dataset_ids: list[str]) -> dict[str, str]:
    """每数据集新鲜度：``fresh`` | ``stale`` | ``unknown``（读时计算不落库）。

    客户隔离：不属 ``customer_id`` 的数据集一律 ``unknown``（不泄露他企
    判定结果）。本函数为跨切片契约（资产目录列表端点直接导入），自管
    会话、只读、绝不抛异常。
    """
    db = get_session()
    try:
        now = _utcnow_naive()
        statuses: dict[str, str] = {}
        for dataset_id in dataset_ids:
            dataset = db.get(Dataset, dataset_id)
            if dataset is None or dataset.customer_id != customer_id:
                statuses[dataset_id] = "unknown"
                continue
            statuses[dataset_id] = _freshness(
                _cadence_hint(db, dataset_id),
                _last_refresh_at(db, dataset),
                now=now,
            )
        return statuses
    except Exception:  # noqa: BLE001 - 契约面：调用方拿不到判定也不该炸
        logger.exception("freshness_statuses 计算失败（如实全量 unknown）")
        return {dataset_id: "unknown" for dataset_id in dataset_ids}
    finally:
        db.close()


# ------------------------------------------------------------- 聚合视图（端点用）


def quality_summary(db: Session, customer_id: str, dataset_ids: list[str]) -> dict:
    """列表徽章视图：每数据集 {freshness, last_refresh_at, row_count, status}。

    ``row_count``/``status`` 取最新一条快照（无论成败——失败态如实给
    ``status=failed``、``row_count=None``，绝不用旧快照行数冒充）。
    """
    now = _utcnow_naive()
    statuses: dict[str, dict] = {}
    for dataset_id in dataset_ids:
        dataset = db.get(Dataset, dataset_id)
        if dataset is None or dataset.customer_id != customer_id:
            statuses[dataset_id] = {
                "freshness": "unknown",
                "last_refresh_at": None,
                "row_count": None,
                "status": None,
            }
            continue
        latest = latest_snapshot(db, dataset_id)
        last_refresh = _last_refresh_at(db, dataset)
        statuses[dataset_id] = {
            "freshness": _freshness(
                _cadence_hint(db, dataset_id), last_refresh, now=now
            ),
            "last_refresh_at": (
                last_refresh.isoformat() if last_refresh is not None else None
            ),
            "row_count": latest.row_count if latest is not None else None,
            "status": latest.status if latest is not None else None,
        }
    return statuses


def quality_detail(db: Session, dataset: Dataset) -> dict:
    """详情视图：latest（含失败/不支持如实态）+ 历史趋势 + 新鲜度 + 节奏。"""
    latest = latest_snapshot(db, dataset.id)
    history = snapshots_for(db, dataset.id)
    cadence = _cadence_hint(db, dataset.id)
    last_refresh = _last_refresh_at(db, dataset)
    return {
        "latest": snapshot_to_dict(latest) if latest is not None else None,
        "history": [snapshot_to_dict(row) for row in history],
        "freshness": _freshness(cadence, last_refresh, now=_utcnow_naive()),
        "cadence_hint": cadence,
        "last_refresh_at": (
            last_refresh.isoformat() if last_refresh is not None else None
        ),
        "note": DISCLAIMER,
    }
