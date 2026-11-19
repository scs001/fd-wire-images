"""pipeline 切片：控制台管线图（wire-transform-pipeline-view）。

``GET /api/v1/pipeline/graph`` —— 本企业数据集派生图（nodes + edges +
拓扑深度 + 定时重派生徽标 + anomalies 计数）。**零引擎调用**：全部信息
来自本地元数据（datasets 表 + materialization 库的 derived_dataset
登记行），绝不触碰 EngineToolClient——图视图是只读概览，不构成数据面
读写路径。

接线（app.py）::

    from fd_wire_api.pipeline import router as pipeline_router
    app.include_router(pipeline_router)
"""

from __future__ import annotations

from .graph import build_pipeline_graph
from .routes import router

__all__ = ["build_pipeline_graph", "router"]
