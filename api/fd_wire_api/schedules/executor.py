"""调度执行器（wire-refresh-scheduler 1.3）：两类目标的定时执行体。

- ``execute_dataset``：复用 data_plane 既有重派生段（``refresh_derived_dataset``
  + ``cascade_refresh_children``——与手动端点同一实现，行为零漂移；调度语境
  级联恒开，spec：父刷新传导派生子）。
- ``execute_artifact``：复用 artifacts/refresh.py 无头刷新轮（LLM 按各面板
  source 重取数 → 新版本落盘），同手动刷新端点的锁与失败零半写语义。

目标失存（数据集已删、产物已删/归档）抛 :class:`TargetGone`——调度循环
据此**自动删除调度行**（不再空转）。其余执行失败原样上抛，由调度循环记
``last_status=failed + last_error``。

可测性：引擎工厂 / LLM / 工具执行器均有模块级 override 钩子（照
materialization.runner 的 ``_engine_factory_override`` 先例）。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: 引擎工厂 override（测试注入假引擎；None=生产 EngineToolClient 轨道）
_engine_factory_override: Callable[[str], Any] | None = None

#: 看板刷新轮 LLM 出口 override（测试注入脚本化 LlmClient）
_llm_factory_override: Callable[[], Any] | None = None

#: 看板刷新轮工具执行器出口 override（测试注入假 executor）
_tool_executor_factory_override: Callable[[str], Any] | None = None


class TargetGone(RuntimeError):
    """调度目标已删除/归档（或不再属于该客户）——调度行应自动移除。"""


def _engine_factory() -> Callable[[str], Any]:
    """引擎工厂：测试 override 优先，生产走 datasets.ingest 真身。"""
    if _engine_factory_override is not None:
        return _engine_factory_override
    from ..datasets.ingest import get_engine_client_factory

    return get_engine_client_factory()


def _scheduler_principal(customer_id: str):
    """调度器身份（data_plane 语义的合成 principal：console 之外的第三轨）。

    refresh 工具入参不含 actor/channel（引擎按 dataset_key 定位），此身份
    仅用于统一调用形态与编辑记录归属的诚实标注。
    """
    from ..data_plane.auth import DataPlanePrincipal

    return DataPlanePrincipal(
        customer_id=customer_id,
        channel="scheduler",
        actor_id=f"scheduler:{customer_id}",
    )


def execute_dataset(*, customer_id: str, target_id: str) -> dict:
    """执行派生数据集刷新：本体重派生 + 级联子链（visited 防环、失败隔离）。

    返回执行摘要（失败以异常上抛——调度循环记 failed+last_error）。
    """
    from ..data_plane.data_routes import (
        cascade_refresh_children,
        refresh_derived_dataset,
    )
    from ..datasets.db import get_session as get_datasets_session
    from ..datasets.models import Dataset
    from ..datasets import service as datasets_service

    principal = _scheduler_principal(customer_id)
    factory = _engine_factory()
    with get_datasets_session() as db:
        dataset = db.get(Dataset, target_id)
        if dataset is None or dataset.customer_id != customer_id:
            raise TargetGone(f"数据集 {target_id} 已删除或不再属于该客户")
        # 与手动端点同款闸（防御位：建调度后数据集形态不该再变，变了如实失败）
        if dataset.source_kind != "derived":
            raise ValueError(
                f"仅派生数据集可刷新（source_kind={dataset.source_kind}）"
            )
        if getattr(dataset, "storage_tier", datasets_service.TIER_CELL) == datasets_service.TIER_API:
            raise ValueError("api 层数据集不可重派生（档位闸）")
        refresh_derived_dataset(db, dataset, factory, principal)
        cascade = cascade_refresh_children(db, dataset, factory, principal)
    return {"dataset_id": target_id, "cascade": cascade}


def _release(client: object) -> None:
    """释放带 close() 的依赖客户端（生产网关/执行器；测试假件无 close）。"""
    if hasattr(client, "close"):
        client.close()


def execute_artifact(*, customer_id: str, target_id: str) -> dict:
    """执行看板 AI 无头刷新：复用 artifacts/refresh.py 轮 + PUT 语义落新版本。

    同手动端点的单飞锁（进行中再触发 → 本次失败记 last_error）与失败
    零半写语义；产物已删/归档抛 TargetGone（调度行自动清理）。
    """
    from ..ai.db import get_session as get_ai_session
    from ..ai.settings import get_settings as get_ai_settings
    from ..artifacts import refresh as artifacts_refresh
    from ..artifacts import service as artifacts_service
    from ..artifacts.models import Artifact, ArtifactStatus

    try:
        artifact_id = int(target_id)
    except ValueError as exc:
        raise TargetGone(f"artifact 目标 id 非法：{target_id!r}") from exc
    with get_ai_session() as db:
        artifact = db.get(Artifact, artifact_id)
        if artifact is None or artifact.customer_id != customer_id:
            raise TargetGone(f"产物 {artifact_id} 已删除或不再属于该客户")
        if artifact.status == ArtifactStatus.archived.value:
            raise TargetGone(f"产物 {artifact_id} 已归档（archived）")
        if artifact.kind != "dashboard":
            raise ValueError(
                f"产物 {artifact.id} 是 {artifact.kind}，仅仪表盘（dashboard）"
                "支持定时 AI 刷新"
            )
        if not artifacts_refresh.acquire_refresh_lock(artifact.id):
            raise RuntimeError(f"产物 {artifact.id} 正在刷新中（单飞锁被占）")
        try:
            if _llm_factory_override is not None:
                llm = _llm_factory_override()
            else:
                from ..ai.llm import TokenGatewayLlmClient

                llm = TokenGatewayLlmClient()
            if _tool_executor_factory_override is not None:
                tool_executor = _tool_executor_factory_override(customer_id)
            else:
                from ..ai.executor import CellMcpExecutor

                tool_executor = CellMcpExecutor(customer_id)
            try:
                new_spec = artifacts_refresh.refresh_dashboard_spec(
                    current_spec=artifacts_service.current_spec(db, artifact),
                    llm=llm,
                    tool_executor=tool_executor,
                    customer_id=customer_id,
                    max_turns=get_ai_settings().ai_max_turns,
                )
                # 成功才落盘：同 PUT 语义（追加新版本行 + operator 审计）
                artifacts_service.update_spec(
                    db, artifact, spec=new_spec, customer_id=customer_id
                )
            finally:
                _release(llm)
                _release(tool_executor)
        finally:
            artifacts_refresh.release_refresh_lock(artifact.id)
    return {"artifact_id": artifact_id, "version": artifact.version}


def execute_schedule(row) -> dict:
    """按调度行分发到对应执行体（runner 的默认执行入口）。

    dataset → execute_dataset；artifact → execute_artifact；其余为脏数据
    （建行走服务层校验，不该出现）——上抛由 runner 记 failed。
    """
    if row.target_type == "dataset":
        return execute_dataset(customer_id=row.customer_id, target_id=row.target_id)
    if row.target_type == "artifact":
        return execute_artifact(customer_id=row.customer_id, target_id=row.target_id)
    raise ValueError(f"未知调度目标类型：{row.target_type!r}")


def reset_overrides() -> None:
    """清空全部注入钩子（测试收尾用，避免跨用例泄漏）。"""
    global _engine_factory_override, _llm_factory_override, _tool_executor_factory_override
    _engine_factory_override = None
    _llm_factory_override = None
    _tool_executor_factory_override = None
