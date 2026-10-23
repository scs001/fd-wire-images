# alert-evaluation Specification（delta）

## ADDED Requirements

### Requirement: 规则契约与校验

alert_rule 产物 spec MUST符合冻结契约：query（面板查询同形，与看板/试取
同一解析与执行路径）、aggregate{column,func∈sum|avg|min|max|count}（func≠
count 时 column 必填）、condition{op∈gt|gte|lt|lte|eq|neq, value 数值}、
intervalMinutes∈[5,1440]、可选 webhookUrl（http(s) URL）与 cooldownMinutes
（缺省 60）。保存时服务端 MUST校验并 400 如实定位非法字段；query 的 dataset
跨客户/不存在 404。存量不合形 spec MUST不阻塞评估器（记 error 事件受冷却，
绝不崩溃）。

#### Scenario: 非法规则拒
- **WHEN** 保存 intervalMinutes=1 或 func=avg 缺 column 的规则
- **THEN** 400，文案定位具体字段；合法规则保存成功

#### Scenario: 存量不合形
- **WHEN** 评估器遇到 spec 不合形的 alert_rule
- **THEN** 记 error 事件（含原因），评估循环继续，进程不崩溃

### Requirement: 调度评估与状态机

评估器 MUST只评估 published 规则（draft 不武装、archived 即解除）；按
intervalMinutes 周期 due 评估（查询→归约单值→条件判定），状态机 ok⇄firing
边缘转换。alert_states MUST持久化（state/last_value/last_evaluated_at/
last_notified_at）；alert_events MUST append-only（firing/resolved/error/
notify_failed，带归约值与阈值）。归约列非数值或查询失败 MUST落 error 事件
而非静默。

#### Scenario: 越限触发与恢复
- **WHEN** 规则条件从假变真再变假
- **THEN** 事件序列 firing→resolved 各一次，状态随之转换；持续真时不重复
  记 firing 事件

#### Scenario: 试运行零副作用
- **WHEN** 调 POST /api/v1/artifacts/{id}/alert/test
- **THEN** 返回 {value, wouldFire}，不写 alert_states、不发通知、不记事件

### Requirement: 通知

webhookUrl 在则触发/恢复沿 MUST POST（5s 超时，载荷含规则名/artifact id/
状态/归约值/阈值/时间）；持续 firing 且距上次通知超过 cooldownMinutes MUST
再发（防静默）；投递失败 MUST记 notify_failed 事件且不阻塞评估。未配
webhookUrl MUST零外呼（只落状态与事件）。

#### Scenario: 沿触发通知
- **WHEN** ok→firing 沿
- **THEN** webhook 收到一次 POST；恢复沿再收一次；持续 firing 在冷却内
  不重复、超冷却再收

### Requirement: 管理面

`GET /api/v1/alerts/events` MUST客户隔离（artifact_id/limit 可选，时间倒序，
默认近 50 条）；产物页 alert_rule 行 MUST显示状态徽标（未评估/ok/firing/
错误）与最近评估时间，详情区 MUST提供近事事件列表与试运行按钮；非
alert_rule 产物 test 端点 MUST 404 如实。

#### Scenario: 事件面
- **WHEN** 客户查询自己的告警事件
- **THEN** 只见本客户规则的事件，倒序分页；他企 artifact_id 不可见
