# Design — wire-copilot-action-gate

## Context

塑造本设计的现状事实（含码点）：

1. 浮层三态（问/做/去壹座）在 `web/src/console/assistant/AssistantConversation.tsx`，
   问走 `POST /api/v1/ai/answer`（`ai/answer.py`，`tools=[]` 结构性无工具），
   做走 `POST .../messages`（`ai/loop.py::run_build_loop`，同步、最多
   `AI_MAX_TURNS=8` 轮）。
2. 构建轨五类产物工具（`ai/tools.py`）"执行"= 把 spec 暂存进
   `ai_draft_artifacts`（`stage_artifact`，入参校验即闸）；数据探索工具经
   `CellMcpExecutor` 打企业 cell MCP（`fd-wire-cell-<cid>/mcp`，平台服务凭据
   轨道，AI 探索不计费）。
3. 草稿转正唯一入口 `POST /internal/materialization/{id}/promote`
   （`materialization/routes.py`，`X-Internal-Token` fail-closed）——客户面
   够不着；前端 `fetchArtifacts`（`web/src/console/api.ts:298`）是死代码。
4. 审批/角色既有事实：`console/auth.py` 的 viewer 写拒（`VIEWER_DENIED_METHODS`
   作用于 HTTP 方法）；多队身份 = `member_active_team` 活跃指针优先。
5. 多库切分：AI 切片（会话/草稿）自有库；datasets、artifacts、billing 各自
   库；跨切片读照 `ai/routes.py` 的双库/三库会话注入纪律。alembic 链头 0038。
6. 前端无 WS/SSE 栈，纯 hash 路由 SPA + vitest；`DashboardRenderer` 可按
   spec 内联渲染（`web/src/console/dashboard/DashboardRenderer.tsx`）。
7. 壹座侧同构参照：paas `add-line-boards-and-action-gate`（2026-10-20 实施）
   ——注册表/三级确认/审计/回执不冒充结果的语义以它为准；其 Non-Goals 明确
   不做 DOM 级界面操控、不引入 CopilotKit 运行时，柏讯沿同一裁决。
8. 两个操作 MCP 仓（`finddata/wire-ops-mcp`、`finddata/wire-build-mcp`）各持
   `tool-surface.json`，写类工具已在 commercial 面上牌。

## Goals / Non-Goals

**Goals**

- 副驾从「只能指路」升级为「提议-确认-执行-审计」，与壹座侧动作闸语义一致。
- 问/做合并为单一副驾：免费档净增能力（读自有数据 + 提议），构建类仍由
  引擎 preflight 把关。
- 草稿看得见、转正点得动（收口 G1 死代码与客户面缺口）。
- 写操作不可绕过：工具面结构性不含写工具，写只存在于闸的单一执行路径。

**Non-Goals**

- 不做 DOM 级界面操控、不引入 CopilotKit/AG-UI 运行时（与壹座侧裁决一致）。
- 不做 SSE/流式（`wire-assistant-panel` D1 裁决维持；提议制把长执行拆到
  请求外，比原来更不需要流式）。
- 数据面 Key 铸造/吊销、团队管理类动作不进 v1（后续变更扩容）。
- 不动壹座侧任何代码（壹座侧的柏讯看板/注册表/pack 是 paas 仓的另一个
  change，按惯例在 paas 会话开）。
- 不新增对外通知通道（wire 无 bot 面）；异步审批靠站内可见 + 24h 过期
  fail-closed。

## Decisions

### D1 — 单一副驾循环取代两轨；`/answer` 保留为兼容子集

`run_build_loop` 演进为 `run_copilot_loop`：工具面 = **内部读工具 + cell 工具
（有引擎才注入）+ `propose_action`**。`answer_question` 的纯问答面保留
（`/api/v1/ai/answer` 行为不变，前端停用）——它是既有对外端点，删除是
breaking；统一循环在工具面为空时自然退化为同款纯问答。

免费档能力扩容的依据：datasets/artifacts/demands 的读对任意档位本就开放
（客户面 REST 无引擎门），把它们做成内部读工具不引入新的越权面。

### D2 — 不可绕过性 = 工具白名单结构性排除（比壹座侧更强）

壹座侧靠聚焦模式 + allowTools 收窄（用户可切 preset 离开副驾）。柏讯拥有
循环本身：`run_copilot_loop` 组装 tools 时**只有**读工具 schema 与
`propose_action` schema，代码路径上不存在写工具可达的分支。写执行只存在于
闸的执行器（D5）。既有五类 spec 工具从模型直调改为 `wire.artifact.draft.create`
提议的参数校验逻辑（`tools.py` 的校验函数原样复用）。

### D3 — 注册表 v1 十一条（冻结契约）

`ai/actions.py` 持注册表；每条 `{id, risk, tier, executor, preflight, impact}`：

| # | action id | 执行体（service 直调） | risk / tier | preflight |
|---|---|---|---|---|
| 1 | `wire.artifact.draft.create` | `stage_artifact`（五类 spec，kind 参数化；校验沿用 `tools.py`） | write / inline | cell 已开通；spec 校验通过 |
| 2 | `wire.dataset.create` | datasets 建集（free 档 CSV 分流，绝不编造示例行） | write / inline | 档位配额（free 3 表/1 万行由底层校验兜底） |
| 3 | `wire.artifact.promote` | `artifacts.service.import_from_draft`（建档，产物页可见）+ `materialization.service.promote_draft`（登记物化/调度）——**经闸开客户面路径**，见 D5 | write / inline | 草稿属本队；kind 可物化 |
| 4 | `wire.artifact.publish` | artifacts publish 状态迁移 | write / inline | 产物属本队；非 viewer |
| 5 | `wire.artifact.share.create` | share 令牌创建 | **publish / async** | 产物已 published；非 viewer |
| 6 | `wire.artifact.archive` | artifacts archive | write / inline（影响面标注可逆） | 产物属本队 |
| 7 | `wire.demand.submit` | demands submit | write / inline（合规字段留白，D8） | 合规门字段客户侧完成 |
| 8 | `wire.market.subscribe` | market 订阅 | **cost / second** | 档位允许（approval 档自动转访问申请的既有语义保留） |
| 9 | `wire.market.unsubscribe` | market 退订 | write / inline | 订阅属本队 |
| 10 | `wire.dataset.correct` | corrections append（`source` 声明性入参，归因权威=服务端） | write / inline | 数据集属本队 |
| 11 | `wire.dataset.correction.revert` | corrections revert | write / inline | 修正属本队 |

发布分级（用户已裁决）：**队内 publish = write/行内确认；分享链接与市场
发布 = publish/异步审批**——对外暴露才配异步审批。

风险→档位映射与壹座侧逐字一致：read→auto（读不过闸，是直通工具）、
write→inline、destructive/cost→second、publish→async。

### D4 — 提议端点与两张表（AI 库，alembic 0039）

端点（客户面 Bearer，挂既有 `ai` 路由）：

- `POST /api/v1/ai/actions/propose` `{action_id, args, conversation_id}` →
  201 `{proposal_id, state, tier, impact, missing_fields?}`。read 级不存在
  （读是直通工具）；未注册/预检失败 → 4xx 如实文案，不落 pending。
- `POST /api/v1/ai/actions/{id}/approve` `{compliance?: {source_url,
  public_claim}}` → inline/second 档确认即执行；publish 档要求本队
  owner/admin（viewer 403）。响应 `{state, result?}`。
- `POST /api/v1/ai/actions/{id}/reject`
- `GET  /api/v1/ai/actions/{id}`；`GET /api/v1/ai/actions?conversation_id=`
  （会话内提议列表——面板草稿卡与历史回放的数据源）。

表（append-only，绝不改写既有行；过期/拒绝是状态行新写或状态列单次迁移，
与 corrections 的撤销语义同款）：

- `ai_action_proposals`：id、conversation_id、customer_id、team_id、
  action_id、args_digest（sha256）、args_summary（脱敏摘要，不含凭据/全文
  spec）、risk、tier、state（`pending_confirm|pending_approval|executed|
  rejected|expired|failed`）、impact_text、proposed_by（logto_sub）、
  approved_by、approved_at、expires_at、created_at。
- `ai_action_audit`：id、proposal_id、action_id、team_id、result（ok|error）、
  error_text、duration_ms、created_at。检索维度：team / 时间 / action。

24h 过期扫描：复用既有定时任务基建（refresh-scheduler 同款进程内定时器）
扫 `pending_approval`，置 `expired`，fail-closed；时钟可注入（测试）。

### D5 — 执行器直调 service 层，跨切片多库纪律；promote 开客户面路径

执行体在 fd-wire-api 进程内直调各切片 service 函数（datasets/artifacts/
share/market/demands/materialization），**不经 HTTP、不经 MCP、不经对话循环
的 ToolExecutor**。跨切片会话按 `ai/routes.py` 的注入纪律各取各库。cell
数据探索保持 `CellMcpExecutor` + 平台服务凭据轨道不变（计量归属不动）。

草稿转正客户面路径 = 闸执行器内部调两条既有 service：`import_from_draft`
产生 `artifacts` 行（spec「转正后出现在产物页」的对象），`promote_draft`
登记物化与调度（计费读取来源）——只登记物化会让草稿在产物页无处可寻
（真机验收 6.2 实证的缺口）。执行体先查物化登记行再建档，二次转正如实
失败且不产生第二份产物。**不新增绕闸 REST 端点**；产物页的手动转正按钮
（若后续要加）走同一条 propose→approve。

审计边界：闸审计记**对话侧**（谁提议、谁批）；`corrections` 体系继续记
**数据侧**（改了哪行、可撤销）。两层并存不重复。

### D6 — 确认卡 = 预览卡二合一

`ActionCard`（`web/src/console/assistant/ActionCard.tsx`）：pending 面显示
动作名/目标/影响面/确认取消；`wire.artifact.draft.create` 的 dashboard kind
在同一卡内用 `DashboardRenderer` 渲染 spec（form/record-view 呈现结构摘要：
字段清单与键列）。resolved 面显示结果摘要与审计指向。历史回放：
`GET /actions?conversation_id=` 的状态驱动卡片两面。

会话草稿列表：复活 `fetchArtifacts`（死代码转正），面板内聚合展示本会话
草稿与预览入口。

### D7 — 多队审批语义

提议落库时 `team_id` 取自活跃队（`member_active_team` 指针）。审批人校验：
inline/second 档 = 本队任意非 viewer 成员（确认者即批准者，记
`approved_by`）；publish 档 = 本队 owner/admin（复用 team-spaces 的角色解析
`resolve_member_role`；viewer 403 且提议状态不变）。

### D8 — 需求单合规门在卡上留白

`wire.demand.submit` 的提议参数里模型可以给业务字段，但确认卡把**公开源
URL 与公开数据声明**渲染为客户必填的空输入；服务端 `approve` 收
`compliance` 字段，模型给的取值一律不采纳（落库前丢弃）。这是
wire-ops-mcp「422 逐字段透传、绝不代勾」纪律在对话面的延续。

### D9 — 执行成功 → 受影响页重取（无 WS）

闸执行成功后前端广播 `window` 级 CustomEvent（`wire:action-executed`，
detail 带 `resources: ["artifacts"|"datasets"|"demands"]` 与 team 标识）；
`ArtifactsPage` / `DatasetsPage` 挂载期监听并重取受影响列表，保留筛选与
滚动。wire 前端无 WS 栈，不为此引入；轮询兜底不需要（事件与执行同页
同步发生）。

### D10 — 风险单一真源 = tool-surface.json `tool_risk` 平行键

两个操作 MCP 仓的 `tool-surface.json` 增顶层 `tool_risk` 映射
（`{"wire_market_subscribe": "cost", ...}`）——additive 新键，不动既有
`tool_groups[].tools` 字符串数组形状，selfcheck 不破。本仓注册表对拍测试
读该文件（构建期拷贝或路径引用），不一致即测试失败。壹座侧注册表（另一
change）消费同一份。v1 由本仓任务组在外部仓落此标注（参照 wanxing-ops-mcp
加 deploy 工具时的跨仓任务先例）。

### D11 — system prompt 换副驾 persona

`answer.py` 的「你不能执行任何操作」句退役；`loop.py` 的 SYSTEM_PROMPT 改
写为副驾 persona，纪律段照壹座官方 pack persona 的写法：只读直通、写必
提议、回执转述不加工（pending 不说成成功）、意图不清先追问、影响面如实。
实体图谱注入块（`entity_graph_prompt_block`）原样保留。

## Risks / Trade-offs

- **[循环改造动到既有 8 处测试]** 提议制的循环仍是同步短循环（模型产出
  `propose_action` 调用 → 写 proposal 行 → 返回 pending，**不等待确认**），
  同步语义不变；改造点集中在 tools 组装与新增一种工具结果，测试破坏面
  收窄。
- **[异步审批无人批]** 站内可见 + 24h fail-closed；无通知通道是已知代价，
  后续接 bot/邮件再补。
- **[模型伪造提议参数]** args 经注册表 schema 校验（复用 tools.py 校验 +
  各 service 的 422 透传）；合规字段服务端强制不采纳模型值（D8）。
- **[两闸漂移（柏讯/壹座）]** 同 id 同风险 + tool-surface.json 单一真源 +
  对拍测试（D10）。
- **[审计膨胀]** proposals/audit append-only 只增不改；参数存摘要不存全文
  spec（spec 在草稿表已有正本）。
- **[免费档读工具被当免费推理通道]** 读工具返回结构化数据非自由文本生成；
  会话日限额与 MAX_TURNS 既有护栏继续生效。

## Migration Plan

1. 后端先行（additive）：注册表 + 端点 + 两表（迁移 0039）+ 循环工具面
   改造 + 单测；旧端点行为不变。
2. 前端随后：模式切换退役、ActionCard + 预览、草稿列表、事件刷新。
3. 外部仓：wire-ops-mcp / wire-build-mcp `tool_risk` 标注 + 对拍测试接线。
4. 回滚：回滚 api/web 镜像即回到现状；两表与新端点留存无害（无消费者）。

## Open Questions

- 产物页是否补一个非对话入口的「转正」按钮（走同一条闸）——面板内转正
  已闭环，按钮属体验增项，实施时看顺手程度。
- 审批过期扫描的周期（建议 15min，与 refresh-scheduler 心跳对齐）。
- 壹座侧 change 的具体拆名（paas 仓），等本 change 归档后开。
