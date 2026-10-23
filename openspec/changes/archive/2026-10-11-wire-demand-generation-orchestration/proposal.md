# Proposal: wire-demand-generation-orchestration — 需求单→爬虫生成编排（合规门+工单投递+状态回流）

## Why

需求单→爬取是柏讯「企业数据汇聚」北极星的最后一座人工桥：两端已通（wire
需求单三态+交付自动开 scope；fd-industry-data 生成流 health-loop 工单契约
+萬星 agent 在役、nmc-weather 全链实证），中间仍是人工批处理。用户已定
合规规则（2026-10-11）：**只要公开数据都可以代爬**——合规审查门第一次有
了明确判据。本 change 把桥接自动化：合规门 → 生成工单 → 投递在役生成流
→ 状态回流 → 客户可见进度，人工门（人审 merge/点亮）原样保留。

## What Changes

- **合规门（intake 强化）**：需求表单新增「数据源地址」（≥1 个 URL）与
  「公开数据声明」（必勾）；服务端硬校验——URL 必须 http(s)、不得内嵌
  账密（user:pass@ 形态即拒）、声明必在。拒绝路径复用既有 rejected 语义
  与文案口径，不新增状态。
- **工单生成（operator 动作）**：in_progress 需求单上「转生成工单」——
  确定性映射为 health-loop 生成工单 YAML（kind=generate、unit=按源域名
  派生 slug、brief={source_urls, expectations, cadence, notes} 取自需求单
  字段、golden 声明即强制、verify 挂 health_verify）；产物落
  pipeline_events（append-only：workorder_exported，含工单全文与哈希）。
- **工单投递（舰队拉取器，凭据反转）**：wire 暴露内部拉取端点
  （FD_WIRE_INTERNAL_TOKEN）：`GET /internal/demands/workorders/pending`
  + `POST /internal/demands/workorders/ack`（幂等）；fd-industry-data 侧
  新增拉取工作流（自托管 runner）拉工单→写 `reports/health-tickets/`→
  GITHUB_TOKEN 自推 main→回 ack（wire 侧记 pipeline_run 事件）。wire 仓
  零外部 git 凭据（deploy key 被 org 禁、PAT 需交互铸造的调研定案）；
  拉取器未部署时控制台展示工单全文供人工推送（诚实降级）。
- **状态回流（公开 raw 免凭据）**：lifespan 同步任务（metering driver
  同构）：拉 github raw 工单文件 + last-run.json → 工单状态迁移映射为
  pipeline_events + 客户可见时间线条目（已投生成/PR 已开/人审中/已合并
  点亮）；PR 链接留档。绝不把内部细节（agent 回执全文）漏给客户面。
- **客户进度可见**：需求详情时间线展示编排阶段（客户安全子集）；终态
  交付仍走既有 deliver 端点（scope 开通）——**编排不自动交付**，操作员
  确认数据到位后人工交付（v0 交付 SLA 语义不变）。

## Impact

- **specs**：demand-intake MODIFIED（表单轻量三项→含源地址+公开声明）；
  新能力 demand-orchestration（工单生成/投递/回流/可见性四要求）。
- **api**：demands/ 新增 orchestration.py（工单映射+投递客户端+同步循环）
  + routes 扩展；schemas 增字段；迁移无（复用 pipeline_events JSON 列）。
- **web**：需求表单两字段 + 详情编排时间线 + 工单查看/复制降级面。
- **部署**：fd-wire-env 增 INDUSTRY_GIT_SSH_KEY（可选；缺省=导出人工模式）。
- **外部契约**：只消费 fd-industry-data 既有 health-loop 契约（YAML 形状、
  tickets 目录、状态枚举），不改对方仓一行。

## Non-Goals

- 直接 A2A 调萬星（v2：sub2api 服务键自铸+私有 agent 白名单后切换，投递
  层接口已为此留缝）。
- 自动 merge/写 schedule/自动交付——agent 与平台侧人审红线原样（fd-industry-data
  「权限边界（不可协商）」对齐）。
- 需求单状态机改动（三态不动；编排进度走时间线不是状态）。
- 源可达性自动探测（v0 不做——评估期人工/后续 v1.x 再加）。
