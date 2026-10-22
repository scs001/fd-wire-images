"""alerts 包：告警规则执行引擎（wire-alert-evaluation）。

本 ``__init__`` **刻意保持轻量**（只再导出 db/models 零环面）：
``artifacts.service`` 保存路径 import 本包的 ``spec_validate``（alert_rule
kind 分派校验）——若此处再急切 import ``routes``/``evaluator``，会经
``artifacts.service`` 部分初始化态绕回成环。路由/评估器由 app 与测试经
子模块直接引入（``from .alerts.routes import router``、
``from .alerts.evaluator import tick_once``）。

对外能力（经子模块）：

- ``alerts.spec_validate.validate_alert_spec``：规则契约纯函数校验；
- ``alerts.db.init_db``：alert_states/alert_events 幂等建表兜底（生产走
  alembic 0028）；
- ``alerts.evaluator``：``tick_once`` 评估循环体 + lifespan start/stop；
- ``alerts.routes``：/api/v1/alerts 管理端点 + 产物试运行端点。
"""

from .db import init_db
from .models import AlertEvent, AlertState

__all__ = [
    "AlertEvent",
    "AlertState",
    "init_db",
]
