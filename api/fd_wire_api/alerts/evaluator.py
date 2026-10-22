"""告警评估引擎（wire-alert-evaluation 任务 2）。

照 schedules/runner tick 范式（模块级 ``_task`` 句柄 + start/stop +
``asyncio.to_thread`` 下放阻塞 IO），每轮 :func:`tick_once`：

1. ai 会话扫 ``kind=alert_rule AND status=published`` 产物（draft 不武装、
   archived 即解除）；
2. 逐条 due 判定（无状态行或 ``now − last_evaluated_at ≥ intervalMinutes``）；
3. 评估：spec 契约校验（不过 → error 事件）→ 查询（复用
   ``artifacts.query_contract._query_dataset``——免费档本地/cell 档引擎同一
   执行路径，评估口径=刷新口径）→ 归约单值 → 条件判定 would_fire；
4. 状态机**边缘触发**（ok ⇄ firing）：翻转记 firing/resolved 事件 + 通知；
   同态 firing 且距上次通知超过 cooldown 再通知（不记事件）；首评 ok 静默；
5. 通知=webhook POST（5s 超时；未配 webhookUrl 只落事件零外呼）；投递
   失败记 notify_failed 事件不阻塞评估；
6. error/notify_failed 事件按冷却窗去重（同规则同 kind 距上次 ≥ cooldown
   才再记，防刷屏）；每条评估完写 last_evaluated_at/updated_at（error 路径
   同样推进——下轮按 interval 重试）。

测试面：``tick_once`` 是同步函数可直接调（注入 ai 会话 / datasets 会话 /
引擎工厂 / 时钟）；出站 webhook 是模块级 :func:`deliver_webhook`，测试
monkeypatch 之（绝不真打网）。
"""

from __future__ import annotations

import asyncio
import logging
import operator
from datetime import datetime, timezone

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai.db import get_session as get_ai_session
from ..ai.models import utc_now_naive
from ..artifacts import query_contract
from ..artifacts import service as artifacts_service
from ..artifacts.models import Artifact
from ..datasets import api_store
from ..datasets.db import get_session as get_datasets_session
from ..datasets.ingest import (
    EngineCallError,
    EngineClientFactory,
    EngineUnavailable,
    get_engine_client_factory,
)
from .models import EVENT_DETAIL_LIMIT, AlertEvent, AlertState
from .settings import get_settings
from .spec_validate import (
    DEFAULT_COOLDOWN_MINUTES,
    INTERVAL_MAX,
    INTERVAL_MIN,
    normalize_alert_spec,
    validate_alert_spec,
)

logger = logging.getLogger(__name__)

#: 条件算子 → 比较函数（condition.op 白名单由 spec_validate 把守）
_OPS: dict[str, object] = {
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
    "eq": operator.eq,
    "neq": operator.ne,
}


def deliver_webhook(url: str, payload: dict) -> None:
    """出站 webhook 投递（POST JSON，5s 超时；非 2xx 视为失败）。

    httpx 是仓内既有依赖（datasets/ai/billing 等切片同款出站面）。模块级
    函数——测试 monkeypatch 本函数即可拦截全部出站（绝不真打网）。
    """
    with httpx.Client(timeout=5.0) as client:
        response = client.post(url, json=payload)
        response.raise_for_status()


# ------------------------------------------------------------- 查询与归约


def _run_query(
    datasets_db: Session,
    *,
    customer_id: str,
    query: dict,
    engine_factory: EngineClientFactory,
) -> tuple[dict | None, str | None]:
    """执行一次规则查询（同一执行路径=刷新口径）；返回 (快照, 错误文案)。"""
    try:
        result = query_contract._query_dataset(
            datasets_db,
            dataset_id=query["dataset_id"],
            customer_id=customer_id,
            query=query,
            engine_factory=engine_factory,
        )
    except HTTPException as exc:  # _dataset_or_404：不存在或他企（不泄露存在性）
        message = (
            f"数据集不存在: {query['dataset_id']}"
            if exc.status_code == 404
            else str(exc.detail)
        )
        return None, message
    except api_store.LocalQueryError as exc:  # 免费窄面拒绝（列/算子/值）
        return None, str(exc)
    except (EngineUnavailable, EngineCallError) as exc:  # cell 未开通/引擎失败
        return None, str(exc)
    if isinstance(result, dict) and result.get("error"):
        return None, f"引擎拒绝: {result['error']}"
    snapshot, error = query_contract._build_snapshot(query, result)
    if error is not None:  # 形状/投影列不存在等快照失败
        return None, error
    return snapshot, None


def _reduce_snapshot(
    snapshot: dict, *, column: str | None, func: str
) -> tuple[float | None, str | None]:
    """快照归约单值；返回 (值, 错误文案)。

    - count=行数（不取列）；
    - 其余逐行取 ``column`` 浮点化（bool 拒——bool 是 int 子类须显式先判，
      经 _build_snapshot 的 bool 已降格字符串、float() 自然失败同归错误面）；
      非数值行 → 「列 X 存在非数值行」；
    - sum 空行集=0.0；avg/min/max 空行集 → 「无行可归约」。
    """
    rows = snapshot.get("rows") or []
    if func == "count":
        return float(len(rows)), None
    names = snapshot.get("columns") or []
    if column not in names:
        return None, f"聚合列 {column} 不在查询结果列中"
    position = names.index(column)
    cells: list[float] = []
    for row in rows:
        cell = row[position] if isinstance(row, list) and position < len(row) else None
        if isinstance(cell, bool):
            return None, f"列 {column} 存在非数值行（布尔值 {cell}）"
        try:
            cells.append(float(cell))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None, f"列 {column} 存在非数值行"
    if not cells:
        if func == "sum":
            return 0.0, None
        return None, "无行可归约"
    if func == "sum":
        return sum(cells), None
    if func == "avg":
        return sum(cells) / len(cells), None
    if func == "min":
        return min(cells), None
    return max(cells), None


def evaluate_once(
    datasets_db: Session,
    *,
    artifact: Artifact,
    spec: dict,
    engine_factory: EngineClientFactory,
) -> tuple[float | None, bool | None, str | None]:
    """即时评估一条规则（试运行端点用）：查询+归约+判定，**零副作用**。

    ``spec`` 是产物当前信封 spec（调用方经 artifacts.service.current_spec
    取得）。返回 (value, would_fire, error)：error 非 None 时前两者 None。
    只触达 datasets 会话与引擎——不写 states/events、不发通知。
    """
    inner = spec.get("spec") if isinstance(spec, dict) else None
    error = validate_alert_spec(inner)
    if error is not None:
        return None, None, f"规则 spec 非法: {error}"
    params = normalize_alert_spec(inner)
    snapshot, query_error = _run_query(
        datasets_db,
        customer_id=artifact.customer_id,
        query=params["query"],
        engine_factory=engine_factory,
    )
    if query_error is not None:
        return None, None, query_error
    value, reduce_error = _reduce_snapshot(
        snapshot, column=params["column"], func=params["func"]
    )
    if reduce_error is not None:
        return None, None, reduce_error
    return value, bool(_OPS[params["op"]](value, params["value"])), None


# ------------------------------------------------------------ 事件与状态机


def _record_event(
    session: Session,
    artifact: Artifact,
    *,
    kind: str,
    value: float | None = None,
    threshold: float | None = None,
    detail: str | None = None,
    now: datetime,
    dedupe_cooldown_minutes: int | None = None,
) -> bool:
    """追加一条事件（append-only）；带去重窗时同规则同 kind 冷却内跳过。

    返回是否真落了行（去重跳过返回 False）。
    """
    if dedupe_cooldown_minutes is not None:
        last = session.scalar(
            select(AlertEvent)
            .where(AlertEvent.artifact_id == artifact.id, AlertEvent.kind == kind)
            .order_by(AlertEvent.id.desc())
            .limit(1)
        )
        if (
            last is not None
            and (now - last.created_at).total_seconds()
            < dedupe_cooldown_minutes * 60
        ):
            return False
    session.add(
        AlertEvent(
            artifact_id=artifact.id,
            customer_id=artifact.customer_id,
            kind=kind,
            value=value,
            threshold=threshold,
            detail=detail[:EVENT_DETAIL_LIMIT] if detail else None,
            created_at=now,
        )
    )
    return True


def _notify(
    session: Session,
    artifact: Artifact,
    state: AlertState,
    *,
    params: dict,
    state_name: str,
    value: float,
    now: datetime,
) -> bool:
    """webhook 通知（firing/resolved 沿共用）；失败记 notify_failed（冷却去重）。

    返回是否投递成功（成功时推进 state.last_notified_at）。未配 webhookUrl
    零外呼返回 False（不算失败）。
    """
    url = params.get("webhook_url")
    if not isinstance(url, str) or not url:
        return False
    payload = {
        "artifact_id": artifact.id,
        "title": artifact.title,
        "state": state_name,
        "value": value,
        "threshold": params["value"],
        "op": params["op"],
        "at": now.replace(tzinfo=timezone.utc).isoformat(),
    }
    try:
        deliver_webhook(url, payload)
    except Exception as exc:  # noqa: BLE001 - 投递失败不阻塞评估，如实留痕
        logger.warning("告警 %s webhook 投递失败：%s", artifact.id, exc)
        _record_event(
            session,
            artifact,
            kind="notify_failed",
            value=value,
            threshold=params["value"],
            detail=f"{type(exc).__name__}: {exc}",
            now=now,
            dedupe_cooldown_minutes=params["cooldown_minutes"],
        )
        return False
    state.last_notified_at = now
    return True


def _finish_error(
    session: Session,
    artifact: Artifact,
    state: AlertState | None,
    *,
    message: str,
    cooldown_minutes: int,
    now: datetime,
) -> None:
    """error 路径收尾：保证状态行存在 + last_evaluated_at 推进 + error 事件（冷却去重）。"""
    if state is None:
        session.add(
            AlertState(
                artifact_id=artifact.id,
                customer_id=artifact.customer_id,
                state="ok",
                last_evaluated_at=now,
                updated_at=now,
            )
        )
    else:
        state.last_evaluated_at = now
        state.updated_at = now
    _record_event(
        session,
        artifact,
        kind="error",
        detail=message,
        now=now,
        dedupe_cooldown_minutes=cooldown_minutes,
    )


def _spec_interval_minutes(inner: object) -> int | None:
    """从内层尽力取合法 intervalMinutes（due 判定用）；取不到返回 None（视为到期）。

    存量不合形 spec 可能连 interval 都读不出——此时按到期处理，让 error
    事件（受冷却去重）如实留痕。
    """
    if isinstance(inner, dict):
        raw = inner.get("intervalMinutes")
        if (
            isinstance(raw, int)
            and not isinstance(raw, bool)
            and INTERVAL_MIN <= raw <= INTERVAL_MAX
        ):
            return raw
    return None


def _spec_cooldown_minutes(inner: object) -> int:
    """从内层尽力取 cooldownMinutes（error/notify_failed 去重窗用）；缺省 60。"""
    if isinstance(inner, dict):
        raw = inner.get("cooldownMinutes")
        if isinstance(raw, int) and not isinstance(raw, bool):
            return raw
    return DEFAULT_COOLDOWN_MINUTES


# ----------------------------------------------------------------- tick


def tick_once(
    *,
    now: datetime | None = None,
    session: Session | None = None,
    datasets_session: Session | None = None,
    engine_factory: EngineClientFactory | None = None,
) -> dict[str, int]:
    """一轮告警评估：扫 published alert_rule → 逐条 due 判定 → 评估+状态机。

    - ``now``：可注入时钟（测试固定时刻；naive UTC）；
    - ``session``：ai 会话覆盖（测试注入；缺省自建）；
    - ``datasets_session``：datasets 会话覆盖（测试注入；缺省逐规则自建——
      生产分库拓扑下查询必须走 datasets 会话，照 query_preview 注入姿势）；
    - ``engine_factory``：引擎工厂覆盖（测试假件；缺省生产工厂）。

    返回本轮计数摘要。单规则任何异常只记日志不断环（照 schedules runner）。
    """
    moment = now or utc_now_naive()
    stats = {
        "scanned": 0,
        "evaluated": 0,
        "firing": 0,
        "resolved": 0,
        "error": 0,
        "notified": 0,
    }
    ai_session = session if session is not None else get_ai_session()
    own_ai = session is None
    try:
        rules = list(
            ai_session.scalars(
                select(Artifact)
                .where(Artifact.kind == "alert_rule", Artifact.status == "published")
                .order_by(Artifact.id)
            )
        )
        for artifact in rules:
            stats["scanned"] += 1
            try:
                _evaluate_rule(
                    ai_session,
                    artifact,
                    moment=moment,
                    datasets_session=datasets_session,
                    engine_factory=engine_factory or get_engine_client_factory(),
                    stats=stats,
                )
            except Exception:  # noqa: BLE001 - 单规则失败不断环
                logger.exception("告警规则 %s 评估异常（跳过，下轮重试）", artifact.id)
                ai_session.rollback()
    finally:
        if own_ai:
            ai_session.close()
    return stats


def _evaluate_rule(
    session: Session,
    artifact: Artifact,
    *,
    moment: datetime,
    datasets_session: Session | None,
    engine_factory: EngineClientFactory,
    stats: dict[str, int],
) -> None:
    """单条规则的完整评估（due 判定 → 校验 → 查询归约 → 状态机 → 落库）。"""
    spec = artifacts_service.current_spec(session, artifact)
    inner = spec.get("spec") if isinstance(spec, dict) else None
    spec_error = validate_alert_spec(inner)

    state = session.get(AlertState, artifact.id)
    # due 判定（error 路径同样按 interval 重试——interval 读不出则视为到期）
    if state is not None and state.last_evaluated_at is not None:
        interval = _spec_interval_minutes(inner)
        if (
            interval is not None
            and (moment - state.last_evaluated_at).total_seconds() < interval * 60
        ):
            return  # 未到期，本轮跳过

    stats["evaluated"] += 1
    # ① spec 校验不过（存量不合形）：error 事件 + last_evaluated_at 推进
    if spec_error is not None:
        _finish_error(
            session,
            artifact,
            state,
            message=f"规则 spec 非法: {spec_error}",
            cooldown_minutes=_spec_cooldown_minutes(inner),
            now=moment,
        )
        session.commit()
        stats["error"] += 1
        return

    params = normalize_alert_spec(inner)

    # ② 查询（datasets 会话：测试注入 / 生产逐规则自建）
    own_ds = datasets_session is None
    ds = datasets_session if datasets_session is not None else get_datasets_session()
    try:
        snapshot, query_error = _run_query(
            ds,
            customer_id=artifact.customer_id,
            query=params["query"],
            engine_factory=engine_factory,
        )
    finally:
        if own_ds:
            ds.close()
    if query_error is not None:
        _finish_error(
            session,
            artifact,
            state,
            message=query_error,
            cooldown_minutes=params["cooldown_minutes"],
            now=moment,
        )
        session.commit()
        stats["error"] += 1
        return

    # ③ 归约（非数值列/空行集 → error 事件）
    value, reduce_error = _reduce_snapshot(
        snapshot, column=params["column"], func=params["func"]
    )
    if reduce_error is not None:
        _finish_error(
            session,
            artifact,
            state,
            message=reduce_error,
            cooldown_minutes=params["cooldown_minutes"],
            now=moment,
        )
        session.commit()
        stats["error"] += 1
        return

    # ④ 条件判定 + ⑤ 状态机（边缘触发）
    would_fire = bool(_OPS[params["op"]](value, params["value"]))
    if state is None:
        # 首评：建行；would_fire → firing 事件+通知，否则初始 ok 静默
        state = AlertState(
            artifact_id=artifact.id,
            customer_id=artifact.customer_id,
            state="firing" if would_fire else "ok",
            last_value=value,
            last_evaluated_at=moment,
            updated_at=moment,
        )
        session.add(state)
        if would_fire:
            _record_event(
                session,
                artifact,
                kind="firing",
                value=value,
                threshold=params["value"],
                now=moment,
            )
            if _notify(
                session,
                artifact,
                state,
                params=params,
                state_name="firing",
                value=value,
                now=moment,
            ):
                stats["notified"] += 1
            stats["firing"] += 1
    else:
        previous = state.state
        state.last_value = value
        state.last_evaluated_at = moment
        state.updated_at = moment
        if would_fire and previous == "ok":
            state.state = "firing"
            _record_event(
                session,
                artifact,
                kind="firing",
                value=value,
                threshold=params["value"],
                now=moment,
            )
            if _notify(
                session,
                artifact,
                state,
                params=params,
                state_name="firing",
                value=value,
                now=moment,
            ):
                stats["notified"] += 1
            stats["firing"] += 1
        elif not would_fire and previous == "firing":
            state.state = "ok"
            _record_event(
                session,
                artifact,
                kind="resolved",
                value=value,
                threshold=params["value"],
                now=moment,
            )
            if _notify(
                session,
                artifact,
                state,
                params=params,
                state_name="resolved",
                value=value,
                now=moment,
            ):
                stats["notified"] += 1
            stats["resolved"] += 1
        elif would_fire and previous == "firing":
            # 同态 firing：距上次通知 ≥ cooldown 且配了 webhook → 再通知（不记事件）
            notified_due = state.last_notified_at is None or (
                (moment - state.last_notified_at).total_seconds()
                >= params["cooldown_minutes"] * 60
            )
            if params.get("webhook_url") and notified_due:
                if _notify(
                    session,
                    artifact,
                    state,
                    params=params,
                    state_name="firing",
                    value=value,
                    now=moment,
                ):
                    stats["notified"] += 1
    session.commit()


# ----------------------------------------------------------------- 循环


_task: asyncio.Task[None] | None = None


async def _loop() -> None:
    interval = get_settings().alert_tick_seconds
    while True:
        await asyncio.sleep(interval)
        try:
            # tick_once 是阻塞 IO（本地/引擎查询 + 出站 webhook）——下放线程
            await asyncio.to_thread(tick_once)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 失败只记日志，等下一轮
            logger.exception("alert evaluator tick crashed; will retry next interval")


def start_alert_evaluator() -> None:
    """启动评估循环（幂等；需在事件循环内；FD_WIRE_ALERTS_ENABLED=false 关闭）。"""
    global _task
    if not get_settings().alerts_enabled:
        logger.info("alert evaluator disabled by settings")
        return
    if _task is not None and not _task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("no running event loop; alert evaluator not started")
        return
    _task = loop.create_task(_loop())
    logger.info(
        "alert evaluator started (interval=%ss)", get_settings().alert_tick_seconds
    )


def stop_alert_evaluator() -> None:
    """停评估循环（app lifespan 收尾调用；幂等）。"""
    global _task
    if _task is not None and not _task.done():
        _task.cancel()
    _task = None
