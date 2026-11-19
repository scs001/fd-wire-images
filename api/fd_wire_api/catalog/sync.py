"""IndicatorRegistrySync：镜像的唯一写路径（同步管道）。

流程（单事务全量重刷，幂等可重入；重刷与日常同步同一代码路径）：
1. 经 ``RegistryClient`` 全量拉取登记表（``list_registry_entries`` 无增量
   游标，每次都是全量）；
2. **异常防御**：本次拉取总条数 < ``min_total``（缺省 1,000，env
   ``CATALOG_SYNC_MIN_TOTAL``）视为上游异常——中止且不提交，旧镜像原样
   保留（防上游工具异常把目录清空）；
3. 单事务内：以 ``indicator_id``（＝ ``source_db:native_code``）为锚点
   upsert 全部记录，并删除镜像中「本次未出现」的旧条目——对账天然把旧
   拼接格式 ID 的存量一次性退役；
4. 推进水位并记录运行状态（成功/失败均落 ``CatalogSyncState``）。

代码层面镜像表没有任何其他写路径：查询侧（service/routes）只拿得到
``db.readonly_session()``，其写语句会被模型层 guard 拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select

from .client import ConceptRecord, RegistryClient, utcnow
from .db import sync_session
from .models import CatalogSyncState, IndicatorSnapshot
from .settings import settings

_STATE_ROW_ID = 1

_SYNC_FIELDS = (
    "source_db",
    "code",
    "name_zh",
    "name_en",
    "description_zh",
    "description_en",
    "domain",
    "frequency",
    "unit",
    "time_span_start",
    "time_span_end",
    "entity_coverage_count",
    "sample_value",
    "status",
)


@dataclass
class SyncResult:
    """一次同步的汇总（手动触发端点与调度日志消费）。"""

    mode: str = "full"  # 恒为 full（无增量路径）
    fetched: int = 0
    created: int = 0
    updated: int = 0
    removed: int = 0
    total_in_mirror: int = 0
    watermark: datetime | None = None
    errors: list[str] = field(default_factory=list)


def _load_state(session) -> CatalogSyncState:
    state = session.get(CatalogSyncState, _STATE_ROW_ID)
    if state is None:
        state = CatalogSyncState(id=_STATE_ROW_ID)
        session.add(state)
    return state


def _apply_record(session, existing: dict[str, IndicatorSnapshot], record: ConceptRecord, now: datetime) -> str:
    """upsert 一条权威记录；返回 'created' | 'updated'。"""
    row = existing.get(record.indicator_id)
    if row is None:
        row = IndicatorSnapshot(indicator_id=record.indicator_id, synced_at=now)
        session.add(row)
        existing[record.indicator_id] = row
        outcome = "created"
    else:
        outcome = "updated"
    for attr in _SYNC_FIELDS:
        setattr(row, attr, getattr(record, attr))
    row.synced_at = now
    return outcome


class IndicatorRegistrySync:
    """从权威源全量重刷指标镜像（仓库内唯一镜像写入口）。

    ``min_total``：单次拉取的最小可信条数（缺省取
    ``settings.catalog_sync_min_total``，即 1,000）——低于它视为上游异常，
    中止提交、保留旧镜像。测试注入小型假源时给 0/小值。
    """

    def __init__(
        self,
        client: RegistryClient,
        session_factory=sync_session,
        clock=utcnow,
        min_total: int | None = None,
    ):
        self._client = client
        self._session_factory = session_factory
        self._clock = clock
        self._min_total = (
            settings.catalog_sync_min_total if min_total is None else max(0, int(min_total))
        )

    def run(self, force_full: bool = False) -> SyncResult:
        """全量重刷（``force_full`` 参数保留以兼容旧触发端点，如今恒为全量）。"""
        now = self._clock()
        session = self._session_factory()
        try:
            batch = self._client.fetch_concepts()
            result = SyncResult(mode="full", fetched=len(batch.items))

            # 异常防御：上游条数骤减（< min_total）不可信——中止且不提交，
            # 旧镜像原样保留（先于任何写语句，事务内无脏状态可回滚）。
            if result.fetched < self._min_total:
                raise RuntimeError(
                    f"authority fetch too small ({result.fetched} < {self._min_total}); "
                    "refusing to refresh mirror"
                )

            existing = {
                row.indicator_id: row
                for row in session.execute(select(IndicatorSnapshot)).scalars()
            }
            previous_total = len(existing)
            for record in batch.items:
                outcome = _apply_record(session, existing, record, now)
                if outcome == "created":
                    result.created += 1
                else:
                    result.updated += 1

            # 全量对账：本次未出现的旧条目（含旧拼接格式 ID 存量）一律退役
            fetched_ids = {rec.indicator_id for rec in batch.items}
            for indicator_id, row in existing.items():
                if indicator_id not in fetched_ids:
                    session.delete(row)
                    result.removed += 1

            result.total_in_mirror = previous_total + result.created - result.removed
            result.watermark = batch.next_watermark or now
            state = _load_state(session)
            state.last_watermark = result.watermark
            state.last_success_at = now
            state.last_run_at = now
            state.last_status = "ok"
            state.last_error = None
            session.commit()
            return result
        except Exception as exc:
            session.rollback()
            self._record_failure(exc)
            raise
        finally:
            session.close()

    def _record_failure(self, exc: Exception) -> None:
        """失败也要留痕（独立 session，避免污染已回滚的事务）。"""
        try:
            session = self._session_factory()
            try:
                state = _load_state(session)
                state.last_run_at = self._clock()
                state.last_status = "error"
                state.last_error = f"{type(exc).__name__}: {exc}"
                session.commit()
            finally:
                session.close()
        except Exception:  # pragma: no cover - 留痕失败不应掩盖原始异常
            pass
