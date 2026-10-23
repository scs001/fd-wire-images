# wire-dashboard-refresh

## Why

看板渲染/编辑/分享已闭环，但快照是死的——「物化执行」承诺未兑现。本 change 交付
**手动刷新 v1**：published/draft 仪表盘一键重取数，快照更新为新版本。执行通道
**复用 AI 构建的无头刷新轮**（llm+loop+CellMcpExecutor 现成链路）：免与 cell
工具名耦合（AI 按面板 source 自选工具取数），任何 AI 生成过的面板都能刷。
确定性查询契约与定时刷新（refresh_schedule 真调度）= v2。

## What Changes

- **API `POST /api/v1/artifacts/{id}/refresh`**（登录，跨客户 404）：仅
  kind=dashboard；构造无头刷新轮——以既有 ai loop 跑一轮系统指令
  （携带当前 spec：要求按各面板 source 重新取数、更新 snapshot、结构不动、
  仅输出更新后的完整 spec JSON）；取回的 spec 经最小校验→**PUT 语义落新版本**；
  无 cell 客户返回明确档位提示（沿用 CellNotProvisioned 语义）；LLM/引擎失败
  如实 502/503 带原因，**不产生半写版本**。
- **并发防护**：同产物刷新进行中再触发→409（单产物单飞；进程内锁即可 v1）。
- **AI 工具描述补强**：dashboard_spec_create 描述追加「面板 source 建议填
  指标 code/数据集名（刷新依赖此标注取数）」。
- **web**：看板视图（查看态）加「刷新数据」按钮：调用→成功提示新版本号并
  重渲染→失败如实呈现；刷新中 loading 态；draft/published 均可刷、archived 无。

## Capabilities

### New Capabilities

- `dashboard-refresh`: 手动物化刷新——AI 无头刷新轮执行、版本化落盘、
  单飞防护、无 cell 档位提示、失败零半写。

### Modified Capabilities

- `dashboard-renderer`: 查看态提供刷新入口（成功后重渲染新版本）。

## Impact

- **fd-wire/api**：artifacts 模块加 refresh 端点+无头轮封装（复用 ai/loop 的
  服务函数，若 loop 强耦合会话模型则抽最小可复用段）、pytest。
- **fd-wire/web**：DashboardPane 查看态按钮+状态、i18n、测试。
- **成本注记**：每次刷新消耗一轮 LLM 调用（token 网关 deepseek flash，探索
  轨道不计客户账单）；定时刷新与确定性查询 = v2（届时走查询契约+调度器）。
