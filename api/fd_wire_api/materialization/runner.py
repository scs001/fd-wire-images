"""物化运行器：把一个已转正产物按其 spec 读取清单跑一遍数据读取。

## 计量语义（双凭据边界，务必与 ai/executor.py 对照阅读）

本运行器的每一次读数都携带**客户凭据**（``CUSTOMER_CREDENTIAL_MAP``，
经 :mod:`.credentials` 解析）打到 **wire 入口**（``WIRE_ENTRY_URL``）。
wire 入口是 registry 侧的鉴权/逐调用审计面：它看到客户身份 →
audit 流（任务 4.1）自动把 ``rows_returned`` 记成**该客户的计量记录**
→ 结算（任务 4.2）计入客户账单。**平台侧不在此重复记账，也不做任何
抵扣**——本运行器只负责「用对凭据、如实汇总行数」。

与 AI 探索轨道相反：``ai/executor.CellMcpExecutor`` 固定携带平台服务
凭据（``AI_PLATFORM_CREDENTIAL``），其行数归集平台、不进客户账单。
两轨绝不互换：
- 这里缺客户凭据 → :class:`.credentials.CustomerCredentialMissingError`
  → fail-closed 拒绝执行（last_status=failed_no_credential）；
- 绝不「先用平台凭据顶一下」——那会让计费行数凭空消失。

## 失败语义（宽容解析 + 如实记录）

- spec 提取不出非空清单 → failed_spec，不发出任何 HTTP；
- 逐项读数：HTTP 非 2xx / 网络错 / 响应缺 ``rows_returned`` → 该项失败
  （缺 rows_returned 与计量管道的异常纪律同源：不静默猜 0）；
- 全成 = success；部分成 = partial_failure（成功项行数照记）；全败 = failed。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import logging

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..billing.ledger import Ledger
from ..billing.models import Customer
from .credentials import CustomerCredentialMissingError, resolve_customer_credential
from .db import utc_now_naive
from .models import MaterializedArtifact
from .quota import QuotaGuard, SqlQuotaGuard

#: 引擎客户端工厂（模块级可注入：调度链不走 FastAPI DI；测试换假件）
from ..datasets.ingest import (  # noqa: E402
    get_engine_client_factory as _default_engine_factory,
)
from .settings import get_settings
from .specs import ReadItem, SpecReadListError, extract_read_list

logger = logging.getLogger(__name__)

#: 运行状态取值（落 last_status）
STATUS_SUCCESS = "success"
STATUS_PARTIAL_FAILURE = "partial_failure"
STATUS_FAILED = "failed"
STATUS_DISABLED = "disabled"
STATUS_FAILED_NO_CREDENTIAL = "failed_no_credential"
STATUS_FAILED_NO_WIRE_ENTRY = "failed_no_wire_entry"
STATUS_FAILED_SPEC = "failed_spec"


class ArtifactNotFoundError(LookupError):
    """产物不存在或不属于该客户（跨企业一律按不存在处理，不泄露存在性）。"""


@dataclass
class ItemOutcome:
    """单项读数结果。"""

    code: str
    ok: bool
    rows: int = 0
    error: str | None = None


@dataclass
class RunResult:
    """一次物化运行的汇总（同步落 ``materialized_artifacts``）。"""

    artifact_id: int
    status: str
    rows: int = 0
    items: list[ItemOutcome] = field(default_factory=list)
    error: str | None = None
    detail: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_SUCCESS


def _extract_rows_returned(payload: object) -> int | None:
    """宽容提取 rows_returned：顶层 / result 内 / meta 内（任务 1.1 契约字段）。

    提不出返回 None——调用方按「项失败」处理，绝不猜 0。
    """
    if not isinstance(payload, dict):
        return None
    value = payload.get("rows_returned")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    for container_key in ("result", "meta", "metadata"):
        container = payload.get(container_key)
        if isinstance(container, dict):
            value = container.get("rows_returned")
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    return None


class MaterializationRunner:
    """逐项经客户凭据调 wire 入口，运行结果如实落库。

    依赖全部可注入（httpx 客户端 / 凭据映射 / wire 入口 / 额度守卫 /
    时钟），测试全离线。
    """

    def __init__(
        self,
        db: Session,
        *,
        http_client: httpx.Client | None = None,
        credential_map: str | dict[str, str] | None = None,
        wire_entry_url: str | None = None,
        quota_guard: QuotaGuard | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._db = db
        self._credential_map = credential_map
        self._wire_entry_url = (
            wire_entry_url if wire_entry_url is not None else get_settings().wire_entry_url
        )
        self._quota_guard = quota_guard or SqlQuotaGuard(db)
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(timeout=timeout)

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def release_db(self) -> None:
        """关闭持有的 DB 会话（调度器每 tick 新建 runner 时用）。"""
        self._db.close()

    # -- 内部 ------------------------------------------------------------

    def _load(self, customer_id: str, artifact_id: int) -> MaterializedArtifact:
        """按草稿逻辑 id 加载；他企或不存在的口径一致（不泄露存在性）。"""
        artifact = self._db.execute(
            select(MaterializedArtifact).where(
                MaterializedArtifact.artifact_id == artifact_id
            )
        ).scalar_one_or_none()
        if artifact is None or artifact.customer_id != customer_id:
            raise ArtifactNotFoundError(
                f"物化产物 {artifact_id} 不存在或不属于 customer {customer_id}"
            )
        return artifact

    def _record_hardstop(self, customer_id: str, *, now: datetime | None = None) -> None:
        """落账本硬停事件（幂等：每客户每期至多一条；写失败不改变拒绝语义）。

        ``customer_id`` 是物化侧的字符串身份键（=``Customer.sub2api_user_id``），
        与 ``SqlQuotaGuard`` 同一映射口径。
        """
        try:
            customer = self._db.execute(
                select(Customer).where(Customer.sub2api_user_id == customer_id)
            ).scalar_one_or_none()
            if customer is None:
                return
            period = (now or utc_now_naive()).strftime("%Y-%m")
            Ledger(self._db).record_hardstop(
                customer_id=customer.id, period=period, ref="quota_exhausted"
            )
            self._db.commit()
        except Exception:  # noqa: BLE001 - 拒绝语义已生效，留痕失败仅记日志
            self._db.rollback()
            logger.exception("账本硬停留痕失败 customer_ref=%s", customer_id)

    def _record(
        self,
        artifact: MaterializedArtifact,
        *,
        status: str,
        rows: int,
        items: list[ItemOutcome] | None = None,
        error: str | None = None,
        now: datetime | None = None,
    ) -> RunResult:
        """把运行结果摘要落库并返回（last_rows=本次成功行数合计）。"""
        artifact.last_run_at = now or utc_now_naive()
        artifact.last_status = status
        artifact.last_rows = rows
        artifact.last_error = error
        artifact.last_run_detail = {
            "items": [
                {"code": i.code, "ok": i.ok, "rows": i.rows, "error": i.error}
                for i in (items or [])
            ],
            "error": error,
        }
        self._db.commit()
        return RunResult(
            artifact_id=artifact.id,
            status=status,
            rows=rows,
            items=list(items or []),
            error=error,
            detail=dict(artifact.last_run_detail),
        )

    def _read_one(self, item: ReadItem, credential: str) -> ItemOutcome:
        """单项读数：客户凭据 + wire 入口（HTTP 层错误不外抛，转项失败）。"""
        body = {"tool": item.tool, "arguments": {"code": item.code, **item.params}}
        try:
            resp = self._http.post(
                self._wire_entry_url,
                json=body,
                headers={"Authorization": f"Bearer {credential}"},
            )
        except httpx.HTTPError as exc:
            return ItemOutcome(code=item.code, ok=False, error=f"wire 入口不可达: {exc}")
        if resp.status_code >= 400:
            return ItemOutcome(
                code=item.code,
                ok=False,
                error=f"wire 入口返回 {resp.status_code}: {resp.text[:200]}",
            )
        try:
            payload = resp.json()
        except ValueError:
            return ItemOutcome(code=item.code, ok=False, error="wire 入口返回非 JSON")
        rows = _extract_rows_returned(payload)
        if rows is None:
            return ItemOutcome(
                code=item.code,
                ok=False,
                error="响应缺 rows_returned——不猜 0，按失败如实记录",
            )
        return ItemOutcome(code=item.code, ok=True, rows=rows)

    # -- 入口 ------------------------------------------------------------

    def run_artifact_row(
        self, artifact: MaterializedArtifact, *, now: datetime | None = None
    ) -> RunResult:
        """按产物行分派（调度器入口，wire-customer-data-engine 6.1）：
        derived_dataset → 引擎重派生；transform_pipeline → 管线运行服务
        （wire-transform-pipelines D3，按当前版本跑、trigger=schedule）；
        其余 → 既有草稿物化路径。"""
        if artifact.kind == "derived_dataset":
            return self._run_derived_refresh(artifact, now=now)
        if artifact.kind == "transform_pipeline":
            return self._run_transform_pipeline(artifact, now=now)
        return self.run(artifact.customer_id, artifact.artifact_id, now=now)

    def _run_transform_pipeline(
        self, artifact: MaterializedArtifact, *, now: datetime | None = None
    ) -> RunResult:
        """transform_pipeline 调度触发：调管线运行服务（trigger=schedule），
        结果摘要落调度行（完整历史在 pipeline_runs/pipeline_run_steps——
        「下次何时跑」与「跑过什么」两职责分离，design D3）。

        配额硬停在管线步骤创建（``ensure_new_dataset_room``）如实定位到
        步骤（step=quota_exhausted、run=failed）；此处不做平台侧额度前置
        门——管线执行与手动运行同走统一代码路（D5），join 重拉指标的计量
        与额度在 registry 行数门/入口侧生效，平台侧不重复计数。
        """
        if not artifact.enabled:
            return self._record(
                artifact, status=STATUS_DISABLED, rows=0,
                error="产物已停用（enabled=false）", now=now,
            )
        pipeline_id = artifact.ref_key or ""
        from ..datasets.db import get_session as get_datasets_session
        from ..pipelines import service as pipeline_service
        from ..pipelines.models import TransformPipeline

        # artifact 实例可能挂在调用方会话（scheduler tick 加载）——merge 进本
        # runner 会话后再改写，``_record`` 的 commit 才真正落库
        artifact = self._db.merge(artifact)
        engine_factory = globals().get("_engine_factory_override") or _default_engine_factory
        session = get_datasets_session()
        try:
            pipeline = session.execute(
                select(TransformPipeline).where(
                    TransformPipeline.id == pipeline_id,
                    TransformPipeline.customer_id == artifact.customer_id,
                )
            ).scalar_one_or_none()
            if pipeline is None:
                return self._record(
                    artifact, status=STATUS_FAILED, rows=0,
                    error=f"管线不存在或不属于本企业: {pipeline_id}", now=now,
                )
            try:
                run, steps = pipeline_service.run_pipeline(
                    session,
                    pipeline,
                    trigger=pipeline_service.TRIGGER_SCHEDULE,
                    engine_factory=engine_factory,
                )
            except Exception as exc:  # noqa: BLE001 - 调度面如实记 failed 不上抛
                return self._record(
                    artifact, status=STATUS_FAILED, rows=0, error=str(exc), now=now
                )
            rows_total = sum(int(s.row_count or 0) for s in steps)
            return self._record(
                artifact,
                status=STATUS_SUCCESS if run.status == "success" else STATUS_FAILED,
                rows=rows_total,
                error=run.error_text,
                now=now,
            )
        finally:
            session.close()

    def _run_derived_refresh(
        self, artifact: MaterializedArtifact, *, now: datetime | None = None
    ) -> RunResult:
        """derived_dataset 定时重派生：额度门 → 引擎 derive_refresh → 结果落库。

        join 类重派生读平台指标的计量与额度硬停在 registry 行数门/入口侧
        生效（引擎走 wire 入口 + cell 凭据，与产物物化同轨）——此处仅做
        平台侧额度前置门，不重复计数。"""
        if not artifact.enabled:
            return self._record(
                artifact, status=STATUS_DISABLED, rows=0,
                error="产物已停用（enabled=false）", now=now,
            )
        verdict = self._quota_guard.check(artifact.customer_id, now=now)
        if not verdict.allowed:
            if verdict.exhausted:
                self._record_hardstop(artifact.customer_id, now=now)
            return self._record(
                artifact, status=verdict.status or STATUS_FAILED, rows=0,
                error=verdict.reason, now=now,
            )
        dataset_id = artifact.ref_key or ""
        from sqlalchemy import select as _select

        from ..datasets.models import Dataset
        from ..datasets.routes import _ensure_engine_ok
        from ..datasets.ingest import (
            EngineCallError,
            EngineUnavailable,
            TOOL_DERIVE_REFRESH,
            get_engine_client_factory,
        )

        from ..datasets.db import get_session as get_datasets_session

        engine_factory = globals().get("_engine_factory_override") or _default_engine_factory

        session = get_datasets_session()
        try:
            dataset = session.execute(
                _select(Dataset).where(
                    Dataset.id == dataset_id,
                    Dataset.customer_id == artifact.customer_id,
                )
            ).scalar_one_or_none()
        finally:
            session.close()
        if dataset is None:
            return self._record(
                artifact, status=STATUS_FAILED, rows=0,
                error=f"派生数据集不存在或不属于本企业: {dataset_id}", now=now,
            )
        client = engine_factory(artifact.customer_id)
        try:
            result = client.call(TOOL_DERIVE_REFRESH, {"dataset_key": dataset.key})
        except (EngineUnavailable, EngineCallError) as exc:
            reason = str(_ensure_engine_ok(exc))
            return self._record(
                artifact, status=STATUS_FAILED, rows=0, error=reason, now=now
            )
        if isinstance(result, dict) and result.get("error"):
            return self._record(
                artifact, status=STATUS_FAILED, rows=0,
                error=f"引擎拒绝: {result['error']}", now=now,
            )
        rows = int((result or {}).get("rows") or 0)
        run_result = self._record(
            artifact, status=STATUS_SUCCESS, rows=rows, now=now
        )
        # 质量快照钩子（wire-asset-catalog 4.3）：派生重算成功——触发一次
        # 质量快照（triggered_by=materialize）；失败隔离，快照失败绝不影响
        # 物化主流程（结果已在头顶落库）。dataset 为脱离会话的登记行，
        # 钩子只取 id/key/customer_id。
        try:
            from ..quality import service as quality_service  # noqa: PLC0415

            quality_service.notify_dataset_changed(
                dataset, "materialize", client=client
            )
        except Exception:  # noqa: BLE001 - 双保险：主流程绝不因快照中断
            pass
        return run_result

    def run(
        self,
        customer_id: str,
        artifact_id: int,
        *,
        now: datetime | None = None,
    ) -> RunResult:
        """跑一次物化：额度 → 凭据 → 清单 → 逐项读数 → 结果落库。

        ``artifact_id`` 为**草稿逻辑 id**（materialized_artifacts.artifact_id
        列，与 promote 端点路径参数同空间）。
        """
        artifact = self._load(customer_id, artifact_id)
        if not artifact.enabled:
            return self._record(
                artifact, status=STATUS_DISABLED, rows=0,
                error="产物已停用（enabled=false）", now=now,
            )

        # 1) 硬停前置（wire-billing「额度耗尽含产物定时刷新一律拒绝」）
        verdict = self._quota_guard.check(customer_id, now=now)
        if not verdict.allowed:
            if verdict.exhausted:
                # 账本硬停半链（wire-platform-v1 4.4）：额度耗尽不止在此拒绝，
                # 同时落一条 hardstop 账本事件——预检 402（交互）与账本硬停
                # （物化/对账面）由此保持一致口径，控制台经账本可如实展示。
                self._record_hardstop(customer_id, now=now)
            return self._record(
                artifact, status=verdict.status or STATUS_FAILED, rows=0,
                error=verdict.reason, now=now,
            )

        # 2) 客户凭据（fail-closed：缺即拒绝，绝不回落平台服务凭据）
        try:
            credential = resolve_customer_credential(
                customer_id, credential_map=self._credential_map
            )
        except CustomerCredentialMissingError as exc:
            return self._record(
                artifact, status=STATUS_FAILED_NO_CREDENTIAL, rows=0, error=str(exc), now=now
            )
        if not self._wire_entry_url:
            return self._record(
                artifact, status=STATUS_FAILED_NO_WIRE_ENTRY, rows=0,
                error="WIRE_ENTRY_URL 未配置——物化读取 fail-closed 拒绝执行", now=now,
            )

        # 3) 读取清单（宽容解析：提不出清单如实报错，不硬跑）
        try:
            read_list = extract_read_list(artifact.kind, artifact.spec)
        except SpecReadListError as exc:
            return self._record(
                artifact, status=STATUS_FAILED_SPEC, rows=0, error=str(exc), now=now
            )

        # 4) 逐项读数（客户凭据经 wire 入口 → registry audit 自动计量）
        items = [self._read_one(item, credential) for item in read_list]
        ok_items = [i for i in items if i.ok]
        rows_total = sum(i.rows for i in ok_items)
        if not ok_items:
            status = STATUS_FAILED
            error = f"全部 {len(items)} 项读数失败"
        elif len(ok_items) < len(items):
            status = STATUS_PARTIAL_FAILURE
            error = f"{len(items) - len(ok_items)}/{len(items)} 项读数失败"
        else:
            status = STATUS_SUCCESS
            error = None
        return self._record(
            artifact, status=status, rows=rows_total, items=items, error=error, now=now
        )
