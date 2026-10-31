"""FastAPI 入口：`uvicorn fd_wire_api.app:app`。"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import __version__
from .admin import router as admin_router
# 告警评估引擎（wire-alert-evaluation）：/api/v1/alerts 管理端点 + 产物试运行
# + lifespan 后台 tick 循环（alert_states/alert_events 挂 AiBase，alembic 0028）
from .alerts.db import init_db as init_alerts_db
from .alerts.evaluator import start_alert_evaluator, stop_alert_evaluator
from .alerts.routes import router as alerts_router
from .ai import init_db as init_ai_db
from .ai import router as ai_router
# 产物生命周期（wire-artifacts-persistence）：/api/v1/artifacts 全端点
from .artifacts import router as artifacts_router
from .billing.routes import router as billing_router
from .catalog import init_db as init_catalog_db
from .catalog import register_catalog
from .catalog import shutdown_catalog, startup_catalog
from .config import settings
from .console import init_db as init_console_db
from .console import router as console_router
# 团队空间（wire-team-spaces）：邀请兑换入队走 session（routes.py）；此处
# 注册 owner-only 团队管理四端点（/api/console/team*）
from .console.team import router as console_team_router
# 个人偏好（wire-personal-preferences）：GET/PUT /api/console/me/preferences
# （per-sub 个人 UI 指针；任意已映射身份含 viewer，见 console/preferences.py）
from .console.preferences import router as console_preferences_router
from .datasets import connector_router as datasets_connector_router
from .datasets import init_datasets_db
from .datasets import router as datasets_router
# 连接器分发的公开下载面（wire-connector-distribution 任务 1）
from .downloads import router as downloads_router
# 签名分享链接（wire-dashboard-share）：published 仪表盘对外只读链接
from .share import router as share_router
# 源包订阅（wire-source-packs）：/api/v1/packs 全端点
from .packs import init_db as init_packs_db
from .packs import router as packs_router
# 管线图（wire-transform-pipeline-view）：本企业数据集派生图（console 身份）
from .pipeline import router as pipeline_router
# 实体语义层（wire-entity-layer）：/api/v1/entities 全端点（实体+关系声明+
# 图谱；图谱紧凑块注入 AI 构建上下文见 ai/routes）
from .domain_actions import router as domain_actions_router
from .entities import init_db as init_entities_db
from .entities import router as entities_router
# 资产目录（wire-asset-catalog）：数据集资产元数据 + 目录浏览端点
# （表挂 DatasetsBase 走 datasets 库；生产迁移 alembic 0030）
from .assets import router as assets_router
# 数据市场（wire-dataset-marketplace）：四档可见性阶梯（private 默认 /
# link / directed / market）+ 定向授权 + 订阅/审批访问（三表挂
# DatasetsBase；生产迁移 alembic 0034；默认私有——未开放永不进任何面）
from .market import artifact_router as market_artifact_router
from .market.showcase_routes import router as showcase_router
from .market import router as market_router
# 数据集质量快照（wire-asset-catalog 任务组 4）：快照通道 + 新鲜度徽章
# （表挂 DatasetsBase；生产迁移 alembic 0031）
from .quality import router as quality_router
# 变换管线（wire-transform-pipelines）：可组合的版本化变换步骤链——
# derive/join/link 编排 + 输出槽位 + run/step 两级留痕 + 管线级 cron 调度
# （五表挂 DatasetsBase；生产迁移 alembic 0032；步骤执行复用既有原语）
from .pipelines import router as pipelines_router
from .demands import admin_router as demands_admin_router
from .demands import init_db as init_demands_db
from .demands import pipeline_router as demands_pipeline_router
from .demands import router as demands_router
# 工单状态回流（wire-demand-generation-orchestration 任务 3）：轮询公开仓
# 工单 YAML 真实状态 → pipeline_events 追加事件 + 客户可见时间线
from .demands.orchestration_sync import start_workorder_sync, stop_workorder_sync
from .demands.auto_export import start_auto_export_sweeper, stop_auto_export_sweeper
from .materialization import init_db as init_materialization_db
from .materialization import router as materialization_router
from .materialization import (
    start_materialization_scheduler,
    stop_materialization_scheduler,
)
from .provenance import router as provenance_router
from .provenance import init_provenance_db
from .provenance.forwarder import start_forwarder_loop, stop_forwarder_loop
from .provenance.sealing import start_sealer_loop, stop_sealer_loop
from .provenance.selfcheck import start_selfcheck_loop, stop_selfcheck_loop
from .metering.db import ensure_schema
from .metering.driver import start_metering_driver, stop_metering_driver
from .metering.settings import get_settings
from .metering.routes import router as metering_router
from .routes import example
# 统一刷新调度器（wire-refresh-scheduler）：/api/v1/refresh-schedules 端点 +
# lifespan 后台 tick 循环（refresh_schedules 走元数据库，alembic 0023）
from .schedules import init_db as init_schedules_db
from .schedules import router as schedules_router
from .schedules import start_refresh_scheduler, stop_refresh_scheduler
# 工作台聚合页（wire-personal-workbench 任务 1.2）：GET /api/v1/workbench——
# 登录客户信息资产全貌单次往返（packs/datasets/ai/demands 四切片纯读聚合；
# 无新表无 init_db，未配置 lifespan 兜底）
from .workbench import router as workbench_router
# 星研内容供给（xingyan-content-supply）：官网三区 feed 幂等摄取为只读快照
# （xingyan_items/xingyan_claims/xingyan_sync_state 落元数据库，alembic 0046）
# + 公开目录/检索面与 claim 级解析 + 每日摄取循环（写入口唯一是摄取管道）
from .xingyan import init_db as init_xingyan_db
from .xingyan import internal_router as xingyan_internal_router
from .xingyan import public_router as xingyan_router
from .xingyan.runner import start_sync_loop as start_xingyan_sync
from .xingyan.runner import stop_sync_loop as stop_xingyan_sync


@asynccontextmanager
async def lifespan(_: FastAPI):
    """启动钩子：计量表幂等建表（可经 METERING_AUTO_CREATE_TABLES 关闭，生产走 Alembic）。"""
    if get_settings().metering_auto_create_tables:
        ensure_schema()
    # 需求单表建表：迁移链落地前 create_all 兜底（幂等，checkfirst）。
    init_demands_db()
    # 指标目录镜像（任务 6.1）：幂等建表 + 每日同步调度（显式 lifespan 下
    # register_catalog 挂的 add_event_handler 不会触发，故在此显式调）。
    init_catalog_db()
    startup_catalog()
    # 计费（任务 4.2）：billing_* 三表 + 档位 seed（幂等；公开三档 + 旧三档
    # 禁用，见 billing/plans.py）。
    from .billing import init_billing

    init_billing()
    # 控制台域（wire-self-serve-launch 4.3）：console_onboardings 断点簿记表
    # 幂等建表兜底（生产走 alembic 0015）。
    init_console_db()
    # 内部后台（任务 7.1）：审核意见表幂等建表兜底（生产走 0007）。
    from .admin import init_db as init_admin_db

    init_admin_db()
    # AI 对话构建循环（任务 5.4）：对话/消息/产物暂存三表幂等建表兜底
    # （生产走 Alembic 迁移链 0005_ai）。
    init_ai_db()
    # 产物物化计量（任务 5.6）：转正登记表幂等建表兜底（生产走 0009）+
    # cron 调度循环（MATERIALIZATION_INTERVAL 扫描，硬停前置在 runner 内）。
    init_materialization_db()
    start_materialization_scheduler()
    # 计量入账驱动（sub2api 入账管道）：周期「registry 审计摄取 → 当月结算」。
    # REGISTRY_MONGO_URI / SUB2API_ADMIN_KEY 未配置时对应段自动跳过。
    start_metering_driver()
    # 工单状态回流（任务 3.1）：tick 缺省 600s；无在途工单静默；
    # WORKORDER_SYNC_ENABLED=false 可关。
    start_workorder_sync()
    # 统一刷新调度器（wire-refresh-scheduler）：refresh_schedules 幂等建表
    # 兜底（生产走 alembic 0023）+ tick 循环（due 调度串行执行；
    # FD_WIRE_REFRESH_SCHEDULER_ENABLED=false 可关）。
    init_schedules_db()
    start_refresh_scheduler()
    # 动作闸过期扫描（wire-copilot-action-gate）：15min 心跳，读路径惰性过期兜底。
    from .ai.actions import start_expiry_sweeper
    start_expiry_sweeper()
    # 告警评估引擎（wire-alert-evaluation）：alert_states/alert_events 幂等
    # 建表兜底（生产走 alembic 0028）+ tick 循环（published alert_rule 逐条
    # 评估，缺省 30s；FD_WIRE_ALERTS_ENABLED=false 可关）。
    init_alerts_db()
    start_alert_evaluator()
    # 客户数据集（wire-customer-local-data 任务组 2）：登记/同步/修正/上传
    # 表幂等建表兜底（生产走 Alembic 0011）。
    init_datasets_db()
    # 溯源账本（wire-provenance-ledger）：账本表兜底（生产走 Alembic 0012；
    # 含触发器安装）+ 三个后台任务——WAL 转发（引擎事件入账）、epoch 封存
    # （backend=none 时空转）、夜间完整性自检（只读）。
    init_provenance_db()
    start_forwarder_loop()
    start_sealer_loop()
    start_selfcheck_loop()
    # 源包订阅（wire-source-packs）：pack_subscriptions 幂等建表兜底
    # （生产走 alembic 0022；指标切片实时查目录镜像，无自身快照表）。
    init_packs_db()
    # 实体语义层（wire-entity-layer）：entities / entity_relationships
    # 幂等建表兜底（生产走 alembic 0026；零引擎调用——纯元数据声明层）。
    init_entities_db()
    # 受理后自动出单补扫（wire-demand-auto-export）：tick 循环扫
    # in_progress 且无任何出单尝试留档的需求单自动出单（缺省 60s；
    # DEMAND_AUTO_EXPORT_ENABLED=false 可关）。零迁移——事件挂既有
    # pipeline_events JSON 列。
    start_auto_export_sweeper()
    # 星研内容供给（xingyan-content-supply）：快照三表幂等建表兜底
    # （生产走 alembic 0046）+ 每日摄取循环（同指纹短路；上游故障保留旧版
    # 并暴露陈旧标记。XINGYAN_SYNC_ENABLED=false 可关）。
    init_xingyan_db()
    start_xingyan_sync()
    try:
        yield
    finally:
        stop_auto_export_sweeper()
        await stop_xingyan_sync()
        stop_alert_evaluator()
        stop_selfcheck_loop()
        stop_sealer_loop()
        stop_forwarder_loop()
        stop_refresh_scheduler()
        # 动作闸过期扫描线程随应用退出（daemon 兜底，显式停更干净）。
        from .ai.actions import stop_expiry_sweeper

        stop_expiry_sweeper()
        stop_metering_driver()
        stop_workorder_sync()
        stop_materialization_scheduler()
        shutdown_catalog()


app = FastAPI(
    title="fd-wire-api",
    version=__version__ or "0.1.0",
    description="柏讯（wire）平台 API——结算/后台服务骨架",
    lifespan=lifespan,
)

app.include_router(example.router)
app.include_router(metering_router)
# billing 域路由（wire-self-serve-launch 1.3/3.1）：公开档位 + 内部额度查询
app.include_router(billing_router)
# 控制台面（wire-self-serve-launch 4.2/4.3/6.1）：登录配置下发 + 首登自动
# 开户 + 客户自助用量（Bearer ID token 身份）
app.include_router(console_router)
# 团队空间（wire-team-spaces）：GET /api/console/team + 邀请生成/作废 +
# 成员撤销（owner-only；门禁在 console.auth.require_console_owner）
app.include_router(console_team_router)
# 个人偏好（wire-personal-preferences）：语言/落地页/置顶看板三键读写
# （身份=任意已映射成员含 viewer；置顶校验查 ai 库，读时自愈清列）
app.include_router(console_preferences_router)
app.include_router(demands_router)
# 需求单 AI 预评估与人工报价（任务 6.4）：评估独立触发 + 确认台内部视图
app.include_router(demands_admin_router)
# 需求单流水线衔接与私有 scope 交付（任务 6.5/6.6）：流水线事实记录 +
# conformance gate 门禁 + 交付建客户私有 scope + 授权转公开
app.include_router(demands_pipeline_router)
# AI 对话构建循环（任务 5.4）：对话会话 + 构建循环 + 产物暂存端点
app.include_router(ai_router)
# 产物生命周期（wire-artifacts-persistence）：持久化/版本化/状态机
# （表与 ai 切片同引擎，create_all 兜底由 init_ai_db 覆盖；生产走 0021）
app.include_router(artifacts_router)
# 告警评估管理面（wire-alert-evaluation）：状态/事件时间线 + alert_rule
# 试运行（零副作用调试面；状态/事件表与产物同 AiBase 引擎）
app.include_router(alerts_router)
# 指标目录镜像（任务 6.1）：公开目录查询 + internal 同步触发 + 每日调度
register_catalog(app)
# 内部后台（任务 7.1）：客户/账本/需求单派发/目录治理/用量分轨
# （双闸鉴权 wire-self-serve-launch 5.1：X-Admin-Token + Bearer 角色闸；
# 未配置 fail-closed；表迁移走 alembic 0007）
app.include_router(admin_router)
# 产物物化计量（任务 5.6）：draft 转正 + 运行摘要内部端点
# （X-Internal-Token 鉴权，未配置 fail-closed；表迁移走 alembic 0009）
app.include_router(materialization_router)
# 客户数据集（wire-customer-local-data 任务组 2）：客户面（Bearer ID token，
# wire-self-serve-launch 4.5 身份切换）
app.include_router(datasets_router)
# 管线图（wire-transform-pipeline-view 任务 1.2）：本企业数据集派生图
# （零引擎调用纯本地元数据；调度徽标来自 materialization 登记行）
app.include_router(pipeline_router)

# REST 数据面（wire-customer-data-engine）：读写端点 + key 管理面 + 后台只读
from .data_plane import routes as data_plane_keys_routes  # noqa: E402
from .data_plane.data_routes import router as data_plane_router  # noqa: E402

app.include_router(data_plane_router)
app.include_router(data_plane_keys_routes.router)
app.include_router(data_plane_keys_routes.admin_router)
# 连接器面（Bearer fwc_...）与内部只读概览；客户数据读写全经引擎 MCP 通道
app.include_router(datasets_connector_router)
# 连接器分发的公开下载面（wire-connector-distribution 任务 1）：
# /api/v1/downloads/connector/*（manifest + 二进制；无鉴权——下载件不含
# 敏感数据；FD_WIRE_DOWNLOADS_DIR 未配置时诚实 404 并说明原因）
app.include_router(downloads_router)
# 签名分享链接（wire-dashboard-share；form 扩展 wire-form-builder）：POST
# 生成（客户鉴权，仅 published dashboard/form）+ 公开验签面（dashboard 只读
# 渲染；form 只读渲染 + POST submit 公开提交，任一不满足统一 404）；
# FD_WIRE_SHARE_SECRET 未配置时两端点 503 诚实禁用（无表 HMAC 令牌）
app.include_router(share_router)
# 统一刷新调度器（wire-refresh-scheduler）：定时刷新 CRUD（Bearer 身份，
# 派生数据集 / dashboard 产物两类目标；间隔地板与每客户配额见 service）
app.include_router(schedules_router)
# 源包订阅（wire-source-packs）：公开包列表/详情（实时目录切片）+ 登录
# 订阅/退订/我的订阅（pack_subscriptions 走元数据库，alembic 0022）
app.include_router(packs_router)
# 实体语义层（wire-entity-layer）：实体/关系声明 + 同源图谱（console 身份；
# 免费档全功能——零引擎调用的元数据层，AI 上下文注入走 ai/routes）
app.include_router(entities_router)
app.include_router(domain_actions_router)
# 资产目录（wire-asset-catalog）：/api/v1/assets 浏览筛选 +
# /api/v1/datasets/{id}/asset-meta 元数据读写与留痕（console 身份；
# 元数据绝不出公开面——红线 3 的资产维度延伸）
app.include_router(assets_router)
# 数据市场（wire-dataset-marketplace）：/api/v1/datasets/{id}/visibility 与
# grants/access-requests（发布方写面）+ /api/v1/market/* 浏览订阅面
# （市场列表 SQL 层只查 market 档——私有数据永不进结果集）
app.include_router(market_router)
# 产物市场（wire-artifact-marketplace）：/api/v1/artifacts/{id}/visibility
# （发布方）+ /api/v1/market/artifacts/*（浏览/订阅/审批/渲染/复制）——
# 看板与研究报告的公开分享（只读看 + 可复制；快照剥离、敏感度双点闸）
app.include_router(market_artifact_router)
# 匿名橱窗（wire-showcase-market）：/api/v1/showcase/* ——平台自营发布方已上架
# 的 market 档看板，**匿名只读**（无任何身份依赖）。客户产物在物理上不在这条
# 路径的查询范围里（market/showcase.py 三条硬条件）；详情返回完整 spec（含
# 快照——橱窗要画得出图），红线由路由边界而非参数正确性保证。
app.include_router(showcase_router)
# 数据集质量快照（wire-asset-catalog 任务组 4）：/api/v1/quality/summary
# 徽章聚合 + /api/v1/datasets/{id}/quality 详情与手动 refresh
# （计算在 cell 引擎侧，平台侧只收聚合计数——红线 2）
app.include_router(quality_router)
# 变换管线（wire-transform-pipelines）：/api/v1/pipelines 全端点——列表/
# 详情/版本化编辑/立即运行/运行历史/管线级 cron 调度（暂停恢复）
app.include_router(pipelines_router)
# 溯源账本（wire-provenance-ledger 任务组 2）：客户面概览/时间线/状态/调档
# （Bearer 身份，4.5）+ 内部 ingest 入账端点（X-Provenance-Token）
app.include_router(provenance_router)
# 工作台聚合页（wire-personal-workbench 任务 1.2）：控制台首屏六段一次返回
# （datasets/配额/packs/artifacts/alerts/demands；纯读、四切片库独立会话）
app.include_router(workbench_router)
# 星研内容供给（xingyan-content-supply）：/api/v1/xingyan/* 公开只读目录与
# claim 级解析（快照来自官网三区 feed；公开件无客户数据，故与 catalog 同
# 口径不要求身份）+ /internal/xingyan/sync 手动触发摄取
app.include_router(xingyan_router)
app.include_router(xingyan_internal_router)


@app.get("/health")
def health() -> dict:
    """存活探针：部署链（ArgoCD/Caddy）与健康检查消费。"""
    return {"status": "ok", "service": "fd-wire-api", "version": app.version}
