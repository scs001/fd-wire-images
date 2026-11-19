"""管线图组装逻辑（wire-transform-pipeline-view 任务 1.1）。

纯函数 ``build_pipeline_graph(db, customer_id)``：把本企业 datasets 全量
行组装成冻结契约的图响应（nodes/edges/anomalies/truncated）。独立于
路由壳，便于直测。

数据来源与约束：
- **nodes**：datasets 表本企业全量（跨企业行绝不入图——查询就按
  customer_id 过滤，隔离不是事后裁剪而是读取面隔离）。
- **edges**：``source_kind='derived'`` 行的 ``source_ref`` 存父
  dataset **key**（见 data_plane ``_register_derived_child``），命中本
  企业（截断后仍在图内的）某 key → 一条父→子边。op v0 统一
  ``"derived"``：derive（查询派生）与 join（指标合流）在本地血缘登记
  里未区分（引擎侧才知道 kind），本地元数据无从还原，统一标注。
- **depth**：源=0（``source_kind!='derived'``，或 source_ref 未命中任何
  图内 key 的孤儿派生行——孤儿按源处理但计 anomalies+1）；派生行 =
  父深度+1。每行至多一条入边（唯一 source_ref），父链单链，多源 BFS
  逐层定深即可；父链成环（异常数据态）的节点定层不可能——按源层 0
  出现、涉环边跳过计 anomalies，端点绝不 500。
- **schedule_cron**：materialization 库 ``materialized_artifacts`` 的
  定时重派生登记行（kind='derived_dataset'、ref_key=dataset **id**、
  schedule 非空）命中 → cron 值，否则 null。materialization 是**另一个
  库连接**：照 data_plane ``dataset_put_schedule`` 既有先例，端点内开
  短会话查询后关闭，主图走主库会话（不做跨库 join）。
- **上限**：节点 500；超出按 updated_at 倒序截断，truncated 如实返回，
  被截断节点的边一并丢弃（其子行父链断裂，按孤儿口径处理）。
"""

from __future__ import annotations

from collections import deque
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..datasets.models import Dataset

#: 节点上限（契约）：超出按 updated_at 倒序截断
NODE_LIMIT = 500


def _load_schedule_crons(customer_id: str) -> dict[str, str]:
    """本企业派生数据集的定时重派生登记：dataset id → cron。

    materialization 库是独立连接（照 dataset_put_schedule 先例函数内
    导入 + 短会话）；id 倒序取首个非空 schedule——登记路径本身保证每
    数据集至多一行（put_schedule 的 select-then-update），倒序只是对
    异常多行的防御（最新登记优先）。
    """
    # 函数内导入：照 data_plane.dataset_put_schedule 的跨切片手法，
    # 避免 pipeline 切片在 import 期锁死 materialization 的库配置。
    from ..materialization.db import get_session as get_mat_session
    from ..materialization.models import MaterializedArtifact

    mat = get_mat_session()
    try:
        rows = mat.execute(
            select(
                MaterializedArtifact.ref_key,
                MaterializedArtifact.schedule,
            )
            .where(
                MaterializedArtifact.kind == "derived_dataset",
                MaterializedArtifact.customer_id == customer_id,
                MaterializedArtifact.ref_key.is_not(None),
                # schedule 非空才算「有定时」：NULL 行是仅手动触发的登记，
                # 图上不亮调度徽标
                MaterializedArtifact.schedule.is_not(None),
            )
            .order_by(MaterializedArtifact.id.desc())
        ).all()
    finally:
        mat.close()
    crons: dict[str, str] = {}
    for ref_key, schedule in rows:
        crons.setdefault(ref_key, schedule)
    return crons


def build_pipeline_graph(db: Session, customer_id: str) -> dict:
    """组装本企业管线图（冻结契约：nodes/edges/anomalies/truncated）。"""
    datasets = list(
        db.execute(
            select(Dataset).where(Dataset.customer_id == customer_id)
        ).scalars()
    )

    # 截断：>500 按 updated_at 倒序保留最新一批（老数据集让位），被截断
    # 节点的边在后续 key 解析时自然丢弃（按孤儿口径：无图内父行）。
    truncated = len(datasets) > NODE_LIMIT
    if truncated:
        datasets.sort(
            key=lambda d: d.updated_at or datetime.min, reverse=True
        )
        datasets = datasets[:NODE_LIMIT]

    nodes_by_key = {d.key: d for d in datasets}

    anomalies = 0
    edge_pairs: list[tuple[str, str]] = []  # (parent_id, child_id)
    children: dict[str, list[str]] = {}  # parent_id → [child_id]（BFS 邻接）
    roots: list[str] = []
    for dataset in datasets:
        if dataset.source_kind != "derived":
            # 源数据集（upload/connector）：深度 0 起点
            roots.append(dataset.id)
            continue
        parent = nodes_by_key.get(dataset.source_ref or "")
        if parent is None:
            # 孤儿派生行：source_ref 未命中图内任何 key（父行不存在，或
            # 被上限截断）——按源处理 depth=0，异常如实计数（tasks 1.1）
            roots.append(dataset.id)
            anomalies += 1
            continue
        edge_pairs.append((parent.id, dataset.id))
        children.setdefault(parent.id, []).append(dataset.id)

    # 多源 BFS 定深度：根=0，派生行 = 父深度+1。每节点至多一个父
    # （唯一 source_ref），层号无「多父取最小」的歧义，BFS 层即链长。
    depth: dict[str, int] = {root_id: 0 for root_id in roots}
    queue = deque(roots)
    while queue:
        current = queue.popleft()
        for child_id in children.get(current, []):
            if child_id not in depth:
                depth[child_id] = depth[current] + 1
                queue.append(child_id)
    # BFS 后仍无深度的节点：父链成环（环上，或父链经环——派生自环成员），
    # 定层不可能。节点照常出现（深度按源层 0 兜底），涉环边跳过计
    # anomalies——环是血缘登记的异常数据态，如实上报而非炸端点。
    assigned = set(depth)  # BFS 实际定层的节点（涉环节点不在内）
    for dataset in datasets:
        depth.setdefault(dataset.id, 0)

    edges: list[dict] = []
    for parent_id, child_id in edge_pairs:
        # 子定过层 ⟹ 其唯一父必先定层（BFS 自上而下）；子未定层即涉环
        if child_id in assigned:
            edges.append(
                {
                    "from_dataset_id": parent_id,
                    "to_dataset_id": child_id,
                    # v0 统一 "derived"：derive/join 区分未入本地登记
                    # （见模块 docstring），本地元数据无从还原
                    "op": "derived",
                }
            )
        else:
            anomalies += 1

    crons = _load_schedule_crons(customer_id)

    # 节点序稳定（深度升序 + key 字典序）：同企业同数据总返回同图，
    # 便于前端缓存与测试断言
    ordered = sorted(datasets, key=lambda d: (depth[d.id], d.key))
    nodes = [
        {
            "dataset_id": d.id,
            "key": d.key,
            "name": d.name,
            "source_kind": d.source_kind,
            "storage_tier": d.storage_tier,
            "row_count": d.row_count,
            # naive UTC → ISO 字符串（与 data_plane schedule 响应同口径）
            "updated_at": d.updated_at.isoformat() if d.updated_at else None,
            "depth": depth[d.id],
            "schedule_cron": crons.get(d.id),
        }
        for d in ordered
    ]
    return {"nodes": nodes, "edges": edges, "anomalies": anomalies, "truncated": truncated}
