# demand-orchestration 变更：受理后自动出单（wire-demand-auto-export）

## ADDED Requirements

### Requirement: 受理后 SHALL 自动出单（后台补扫）

系统 MUST 以后台 tick 循环（lifespan 挂载，缺省间隔 60s、环境变量可调）
定期补扫「in_progress 且无任何出单尝试留档（既无 `workorder_exported`
也无 `auto_export_failed`）」的需求单，对每单自动调用既有出单 service
（`export_workorder_service`，语义/留档/拒绝口径逐字复用）送入舰队拉取
队列——使受理（submitted→in_progress）后无需任何人工点击即完成出单，
同时兜住受理时点前后的进程重启/瞬时异常。自动出单成功时
`workorder_exported` 事件 MUST 带 `trigger: "auto"` 标注（人工出单不带
此键，既有历史与人工路径零漂移）；被拒时（无源地址/slug 占用/禁爬复检）
MUST NOT 产出工单、MUST NOT 回滚任何状态，MUST 落一条
`auto_export_failed` 事件（含 reason 与 status_code）供运营面如实呈现。
补扫 MUST 幂等：已有任一尝试留档的单（含 `auto_export_failed`——拒绝原因
均为需人工处置类，MUST NOT 重试防事件刷屏）绝不重复动作；单次 tick 中
某单的失败 MUST NOT 中断其余单的补扫与循环本身。循环 MUST 可经环境变量
关闭（缺省开），关闭时受理照常、零出单事件、人工出单入口不受影响。

#### Scenario: 受理后自动出单
- **WHEN** 操作员把带合法源地址的 submitted 单迁移到 in_progress，随后
  补扫 tick 运行
- **THEN** 该单出现 workorder_exported 留档（trigger=auto，含 ticket_path
  与 sha256），且出现在舰队拉取队列（pending 端点可见）；全流程无任何
  人工出单动作

#### Scenario: 出单被拒如实留痕
- **WHEN** 补扫遇到一张无 source_urls 的存量在途单（或 slug 已占用的单）
- **THEN** 落一条 auto_export_failed（reason 含服务端拒绝文案），无
  workorder_exported，需求单状态与其余事件零变化

#### Scenario: 补扫幂等
- **WHEN** 已出单或已有 auto_export_failed 的 in_progress 单经历任意多轮
  tick
- **THEN** 不产生任何新事件（逐字节不变），其余漏网单照常补出

#### Scenario: 关闭开关
- **WHEN** 环境变量把自动出单关闭
- **THEN** 补扫不动作（受理照常成功、零出单事件），人工「转生成工单」
  入口行为不变

### Requirement: 运营进度面 SHALL 呈现自动出单失败与自动标注

admin 派发台进度面 MUST 渲染 `auto_export_failed` 阶段（含失败原因），
操作员可见「自动出单被拒·需人工处置」并据此使用既有「转生成工单」人工
入口重试；`workorder_exported` 为自动触发（trigger=auto）时 MUST 呈现
自动标注。未出单占位（「尚未出单」）MUST 只在无任何出单尝试留档时显示
——有 auto_export_failed 即 MUST NOT 再显示该占位。

#### Scenario: 失败可见
- **WHEN** 操作员展开一张自动出单被拒的需求单进度面
- **THEN** 时间线含「自动出单被拒」阶段与失败原因，且不再显示
  「尚未出单」占位

#### Scenario: 自动成功标注
- **WHEN** 操作员展开一张自动出单成功的单
- **THEN** 「已出单」阶段带自动触发标注，其余阶段渲染不变

#### Scenario: 占位语义
- **WHEN** 操作员展开一张从未有任何出单尝试的 in_progress 单
- **THEN** 进度面显示「尚未出单」占位（原语义不变）
