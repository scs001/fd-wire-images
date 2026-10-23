# Proposal: wire-alert-evaluation（告警规则执行引擎）

## Why

产物六类里的 alert_rule 至今**只有声明**：枚举里有名、UI 能存能看，但零评估、
零调度、零通知——产品面展示一个不工作的类别是诚实面问题。告警（指标越限→
通知）是数据平台基线能力（Bloomberg/Palantir 均有），也是 ROADMAP 方向 D
「个人化编排：我的规则」的中期点名项。本轮把既有声明面变成活的执行面。

## What Changes

**规则契约**（alert_rule 产物 spec.spec 内层，保存时校验、评估器消费）：

```json
{
  "query":  {面板查询同形： dataset_id, columns?, filters?, sort?, limit?},
  "aggregate": {"column": "<数值列>", "func": "sum|avg|min|max|count"},
  "condition": {"op": "gt|gte|lt|lte|eq|neq", "value": <数值>},
  "intervalMinutes": 30,
  "webhookUrl": "https://…（可选）",
  "cooldownMinutes": 60（可选，缺省 60）
}
```

- query 与看板面板/试取**同一契约同一执行路径**（parse_panel_query +
  _query_dataset：免费档本地零引擎、cell 档引擎 query）——评估口径=刷新口径，
  不另立第二套查询面。
- 保存校验（api 裁决 + 形状错误 400 定位字段）：intervalMinutes ∈ [5,1440]；
  func≠count 须带 column；webhookUrl 在则须 http(s) URL；value 须数值。
- 存量 alert_rule 产物 spec 不合形（AI 时代遗留/手写）：**保存新版本才过校验**；
  评估器对不合形 spec 记 error 事件（受冷却约束不刷屏）绝不崩溃。

**评估引擎**（api 后台循环，照 refresh-scheduler tick 范式）：

- 只评估 **published** 规则（draft 不武装、archived 即解除）；
- 每 tick 扫 due 规则（now − last_evaluated ≥ intervalMinutes），逐条串行：
  查询→归约单值（sum/avg/min/max/count；数值列非数值行→error 事件）→
  条件判定→状态机 **ok ⇄ firing**（边缘触发）；
- alert_states 持久状态（state/last_value/last_evaluated_at/last_notified_at）
  + alert_events append-only（firing/resolved/error/notify_failed 四类，带值与
  阈值）；
- 通知=webhook POST（5s 超时；触发/恢复沿时发；仍 firing 且距上次通知超过
  cooldown 再发防静默；投递失败记 notify_failed 事件不阻塞评估）；未配
  webhookUrl 只落事件零外呼。SMTP 邮件=Non-Goal（生产 SMTP 未配）。

**管理面**：

- `GET /api/v1/alerts/events?artifact_id&limit`（客户隔离，时间倒序）；
- `POST /api/v1/artifacts/{id}/alert/test`（试运行：即时评估返回
  {value, wouldFire}，**不落状态不发通知**——人机调试面）；
- web 产物页 alert_rule 行：状态徽标（未评估/ok/firing/错误）+ 最近评估时间；
  详情区近事事件列表 + 「试运行」按钮（显示归约值与 would-fire）。

## Non-Goals

- 告警构建器 UI（规则编撰仍走产物 spec 保存/AI 生成——构建器另立项）；
- SMTP 邮件/多渠道通知（webhook 唯一通道 v0）；
- 多条件组合告警、环比/同比窗口函数（v1+）；
- 新的计量面（评估查询走既有 query 路径，计量口径与契约刷新同构）。

## Impact

- 代码：api 新模块 alerts/（校验+状态机+循环+事件+路由）挂 app lifespan；
  web ArtifactsPage 徽标/事件/试运行。零库迁移负担（新表走幂等 create_all
  兜底惯例 + 迁移链补段）。
- spec：新能力 alert-evaluation 4 要求；artifact-lifecycle 零改动（kind 面不动）。
