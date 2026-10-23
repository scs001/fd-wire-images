# Proposal: wire-demand-auto-export — 受理后自动出单（方向 C 触发自动化）

## Why

ROADMAP 方向 C 的定论是：**中间段编排已在役**（工单 YAML→舰队拉取→萬星生成
→PR→回流→时间线，DE-20261011-99 生产全链实证过），**纯手工的只剩「触发出单」
这一步**。此前它等两个前置：合规门（已落：禁爬清单双闸 + 公开数据声明硬勾 +
源地址形态校验）与萬星凭据（已被拉取架构消解：wire 零外部凭据）。前置齐了，
最后一个手工环节——操作员受理后手动点「转生成工单」——现在可以自动化。

收益：客户提单 → 操作员受理（submitted→in_progress）后**零人工点击**自动出单
入拉取队列，「客户提单到 agent 开工」之间不再需要任何出单动作；出单被拒
（无源地址/slug 占用/禁爬复检）如实留痕，操作员在进度面看到失败原因再人工
处置——自动化与人工兜底并存，绝不静默。

## What Changes

1. **后台补扫自动出单**（api，新模块 `demands/auto_export.py`）：lifespan 挂
   tick 循环（照 workorder_sync 既有范式），定期扫描「in_progress 且无任何
   出单尝试留档」的需求单并自动出单——复用 `export_workorder_service` 逐字
   语义（状态门/源地址/slug 三态探测/禁爬复检/脱敏/append-only 留档）。
   **设计决策：不做同步迁移钩子**——出单含 slug 网络探测（fd-prod 海外出站
   挡死 raw.githubusercontent.com，每次探测 10s 超时），挂在受理请求上会把
   操作员响应拖慢 10 秒；补扫天然覆盖两个受理面（内部端点/管理台）与受理
   时钩子失效的漏网，单机制更简、响应零延迟。环境变量可关（缺省开）、
   间隔可调（缺省 60s）。
2. **出单事件带自动标注**：自动出单成功时 `workorder_exported` 事件加
   `trigger: "auto"`（人工出单不带此键——历史与人工路径零漂移）；被拒时落
   `auto_export_failed` 事件（reason + status_code 如实留档），状态不迁移、
   零半写。同一单已有任一尝试留档即不重试（幂等防刷屏；拒绝原因均为需人工
   处置类）。
3. **进度面呈现**（web+api）：admin 进度面新增 `auto_export_failed` 阶段
   渲染（含失败原因）；`workorder_exported` 带自动标注时呈现「自动」徽标。
   未出单占位（「尚未出单」）在有失败留痕时不再显示。
4. **操作员人工入口保留**：`WorkorderSection` 人工「转生成工单」不动——
   自动失败后操作员可人工重试（如换 slug override）；文案补自动出单说明。

### 边界（显式不做）

- 不做「受理」动作本身自动化（submitted→in_progress 仍是操作员动作——受理=
  人工审查点，本 change 只消掉受理之后的出单点击）；
- 不自动流转 delivered（既有 spec 红线：终态交付由操作员执行，不变）；
- 不做存量补扫的一次性迁移（补扫循环天然覆盖存量在途单，无迁移）；
- 不改工单 YAML 契约/slug 探测/脱敏红线（逐字复用出单 service）；
- 不做同步钩子（见上「设计决策」）。

## Impact

api：`demands/auto_export.py`（新模块：补扫+事件常量+循环）、
`demands/orchestration.py`（`export_workorder_service` 加可选 `trigger` 参，
缺省零漂移）、`app.py`（lifespan 挂循环）、测试新增；web：admin DemandsPage
进度面加一阶段+自动徽标+i18n 双语；零迁移（无表变更）；发布 api+web 滚版；
引擎/cell 零改动。
