"""变换管线切片（wire-transform-pipelines）：可组合的版本化变换步骤链。

- 管线定义版本化且 append-only（改步骤出新版本，只改元信息不动版本）；
- 线性步骤链 derive/join/link（参数形状与既有原子操作端点一致）；
- 输出槽位稳定（同版本重跑=原地刷新；新版本新槽位）；
- run/step 两级运行留痕 + 失败隔离（失败步骤后的剩余步骤不留行）；
- 管线级 cron 调度挂 materialized_artifacts（kind=transform_pipeline）。

表挂 ``DatasetsBase``（生产迁移 alembic 0032，create_all 开发兜底随
datasets init 覆盖——models 经本包导入即注册）。
"""

from __future__ import annotations

from .routes import router

__all__ = ["router"]
