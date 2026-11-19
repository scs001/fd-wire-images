"""星研内容供给切片（xingyan-content-supply）。

把官网三区机读 feed（内参/学院/研究）幂等摄取为平台侧只读快照，使条目与
claim 骨架成为客户域目录的一等可发现对象。

与 ``catalog`` 切片同构（权威源的只读镜像：唯一写入口是同步管道，查询侧
只拿只读语义），但**独立 Base 与独立表**——``catalog`` 的 ``MirrorWriteBlocked``
guard 与 ``IndicatorSnapshot`` 的语义是"指标注册表镜像"，两个权威源混表会让
一条写保护纪律承担两种含义。

纪律：
- 写路径唯一：``sync.XingyanFeedSync``（摄取作业）。请求路径永不可写。
- 上游版本断言：feed 顶层 ``schema_version`` 不在兼容范围即拒摄。
- 失败不冒充空集：任一区拉取失败/半版本即整体回滚，上一版继续服务。
"""

from .db import init_db, readonly_scope, readonly_session, reset_xingyan_runtime
from .routes import internal_router, public_router

__all__ = [
    "public_router",
    "internal_router",
    "init_db",
    "readonly_session",
    "readonly_scope",
    "reset_xingyan_runtime",
]
