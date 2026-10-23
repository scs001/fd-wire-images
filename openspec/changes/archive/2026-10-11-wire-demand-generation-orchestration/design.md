# Design: wire-demand-generation-orchestration

## D1 投递通道为什么是「舰队拉取器」而不是 wire 推送或直连 A2A

萬星生成 agent（spider-heal v6+，kind=generate 分诊）是**私有部署**：
调用需 sub2api sk- 键 + 私有 agent 白名单——两件都要一次性的面板操作
（sub2api 无 admin 造键，键只能用户面板会话自铸）。而 fd-industry-data
的 health-inspect 工作流**已经持有在役凭据并每日消化工单**（「工具出单」
是生成流的正规入口）。投递方向的凭据调研结论（2026-10-11）：github
deploy key 被 FDT org 禁用、PAT 铸造需交互式 web、gitee/github 主干
SHA 分叉不可依赖镜像。因此定案**反转投递方向**：wire 暴露内部拉取端点
（FD_WIRE_INTERNAL_TOKEN——我们自己铸的令牌），fd-industry-data 侧新增
拉取工作流（自托管 runner 免 billing）拉工单→写 tickets 目录→
GITHUB_TOKEN 自推 main（自家仓零新凭据）→回 ack。wire 仓**零外部
git 凭据**。直连 A2A 留 v2（sub2api 服务键自铸+白名单后）。

时延口径：拉取工作流 schedule（建议 */30）→ 需求→投递最坏 ~30min；
SUBMIT 由既有每日巡检代劳（03:17）。对 B2B 需求单（既有承诺「1 个工作日
内响应」）充分；赶急单由操作员手动 workflow_dispatch（gh CLI，运营手册
条目）。

## D2 合规门的「硬」与「软」

用户规则=「只要公开数据都可以代爬」。可机器硬判的：URL 形态（http/https、
无内嵌账密）+ 客户声明（公开数据必勾，表单显式声明不勾即无法提交）。
不可机器判的（登录墙后数据、反爬条款灰色地带）→ 既有评估流人工兜底：
evaluation 的 compliance_flags 已有本地初筛种子（legal-domain-review），
编排侧不重复造。**硬拒路径**只有两条：URL 形态非法、声明缺失——其余
一律进人工评估，不搞自动放行。

## D3 工单映射（需求单字段 → health-loop YAML）

| 需求单 | 工单 | 说明 |
|---|---|---|
| code | 文件名 `<YYYYMMDD>-wire-<code>.yaml` | 目录内可追溯 |
| 源域名 | `unit: spiders/<slug>/` | slug=域名净化 `^[a-z0-9][a-z0-9-]*$`，冲突即拒（仓内已存在=转人工，对齐生成流硬边界） |
| intent + entities_hint | `brief.expectations` | 意图原文+实体提示拼接 |
| source_urls（新增字段） | `brief.source_urls` | 原样（已过合规门） |
| frequency_expect | `brief.cadence` | 映射 daily/hourly/weekly，非法值落 notes |
| company/contact | `brief.notes` 前缀注明来源需求单 | 审计线索 |
| — | `golden: ["spiders/<slug>/golden/"]` | 声明即强制（样本缺失=验证红） |
| — | `verify.commands` | health_verify 标准命令 |
| — | `kind: generate`、`category: null` | 契约要求 |

## D4 状态回流与客户可见性（生产实测校正：推送式）

2026-10-11 生产实测：fd-prod 到 raw.githubusercontent.com 被海外出站挡死
（60s 网络不可达）——slug 探测的 fail-closed 会误伤全部出单，拉取式回流
也不可用。定案**推送式**：拉取器（有网）从 `GET /internal/demands/
workorders/tracked` 取在途工单路径，读本地 checkout 工单文件全文，
`POST /internal/demands/workorders/status` 回推；wire 解析映射（幂等）。
slug 探测降级三态（free/occupied/unverified）：不可达如实放行，占用
复检在拉取器本地 checkout 权威执行。B 节轮询循环保留为出站可达环境
兜底，生产 env `WORKORDER_SYNC_ENABLED=false` 关闭。状态→事件映射：

| 工单 status | pipeline_event | 客户时间线 |
|---|---|---|
| 新投 | workorder_submitted | 已进入生成流水线 |
| pr_open（agent 汇报） | pipeline_run{branch,pr_url} | 爬虫已生成，待人工审核 |
| closed（人审 merge） | gate_result{passed:true} | 审核通过，点亮部署中 |
| failed/rejected | gate_result{passed:false,note} | 生成受阻，运营跟进 |

内部字段（agent 回执全文/重试细节）只进 pipeline_events 原文留档，客户面
（`Demand.delivery` 同款纪律）只出安全子集。终态后操作员用既有 deliver
端点交付——编排绝不自动置 delivered。

## D5 失败与降级语义

- 推送失败（网络/权限/分支保护）：工单不丢——pipeline_events 留
  workorder_exported 全文，控制台可重试推送；连续失败不阻塞需求单其他
  操作。
- 同步循环失败：静默重试下轮（metering driver 同款），绝不写「未知的
  进度」。
- fd-industry-data 重命名 tickets 目录/改契约：拉取 404 → 事件
  workorder_lost 留痕转人工——外部仓不是我们的变更域，宁可停也不猜。

## D6 安全

- 拉取/ack 端点=FD_WIRE_INTERNAL_TOKEN（既有内部令牌口径，401 一致）；
  wire 不持有任何外部仓 git 凭据（投递由对方仓工作流自推自家 main）。
- fd-industry-data 侧拉取工作流的凭据=WIRE_DEMANDS_TOKEN（GHA secret，
  值=wire 内部令牌；repo admin 经 gh secret set 注入，单仓限定）。
- 工单内容=客户源地址+意向文本：不含任何凭据（合规门已拒内嵌账密）；
  公司名/邮箱**不进工单**——PR 是公开仓，客户以脱敏代号（customer_id
  前 8 位）留档。
