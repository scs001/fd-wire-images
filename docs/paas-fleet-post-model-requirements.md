# 萬星/壹座侧需求：舰队岗位级模型（fleet-post-model）

> 来源：柏讯内容舰队（`fd-wire` change `wire-content-fleet`）实施中发现的能力缺口
> 对象：paas（壹座 + 萬星）仓
> 状态：**paas 已立项**（`paas/openspec/changes/add-fleet-post-model`，proposal + design 已就绪，即将实施）。
> 本文是需求源，paas 的 proposal 引用它；实施细节以 paas 的 design 为准，两处冲突时以本文口径为准并回写。
> 与上一份 `wire-content-fleet-requirements.md` 独立，可分开排期。

## 一、问题（一句话）

舰队岗位**无法指定或更换模型**：岗位表 `fleet_members` 没有模型列，回合路径也不接受模型覆盖，导致「巡线岗用便宜模型、内容岗用强模型」这类最基本的成本分层做不到——而这恰恰是舰队存在的意义（按角色分工做不同的活，花不同的钱）。

## 二、现状核查（已实读代码）

| 层 | 现状 | 证据 |
|---|---|---|
| pack 声明 | **只有 a2a 服务能钉**：`serving.model` + `serving.modelWhitelist`（≤8）；普通 chat persona 明确禁 `model` | `lib/pack-manifest.js:177` `AGENT_FORBIDDEN_KEYS`；`:679-706` serving 校验 |
| 部署配置 | a2a 服务可改（节奏/预算/卡片/模型），挂部署不挂版本 | `gateway/packs.js:2433` `PUT /api/packs/:id/deployments/:agentId/config`；spec `agent-service-config` |
| 会话运行时 | 人在对话里可切模型/思考档 | `server/agent-session.js:205-236` `switchEffortToInner`；spec `model-selection` |
| mission | 四元组带 `model` | `fd-wanxing/services/wanxing-facade/missions.js:96-105` |
| **舰队岗位** | **无模型字段**；回合路径无模型参数 | `server/routes/fleets-db.js:37-46` 表结构；`server/cron-runner.js:220` `runPersonaTurn` |

**为什么这不是「包作者钉模型」那条边界**：那条边界（persona 禁止 `model`）挡的是**包作者**往内容包里塞运行时配置，是对的，不该动。这里要的是**cell 主人给自己的岗位选模型**——信任级别与它已经在设的「节奏、KPI 槽、在岗者」完全同级。两件事不能混为一谈。

## 三、需求

### F1 · 岗位模型字段与读写接口

`fleet_members` SHALL 增加可空列 `model`（文本，模型标识符）。岗位的读面 SHALL 返回该值；写面 SHALL 支持在建岗与改岗时设置/清除。

**接口**（沿用现有舰队路由族）：
- `POST /api/fleets/:id/members` 接受可选 `model`
- `PATCH /api/fleets/:id/members/:memberId` 接受可选 `model`（`null` = 清除，回到 cell 默认）
- 所有返回 `memberView` 的读面（舰队详情、岗位详情）SHALL 带 `model`

**校验**（复用既有三闸，不新造）：
1. 标识符形状：`MODEL_ID_RE`
2. 宿主 runner 的模型 roster：`modelChoiceError({model, roster})`
3. 计费车道：若该 cell 已绑计费，走 `modelChoiceError({model, lanes})`

失败 SHALL 如实拒绝并指名哪一闸。

**验收**：
- 建岗带 `model` → 岗位读面回显；`PATCH` 改 → 立刻回显；`PATCH null` → 回显 null；
- 非法标识符 / 不在 roster / 不在车道 → 400 且错误文案指名原因；
- 不带 `model` 建岗 → 行为与现状逐字节一致（列可空，不改变既有语义）。

### F2 · 回合路径按岗位模型执行

成员回合 SHALL 以岗位声明的模型运行；岗位未声明时 SHALL 回落到 cell 默认模型（现状）。

**实现要点**（供参考，不约束实现）：
- `runPersonaTurn` 在 `switchPresetForJob` 之后/之内把岗位模型带进运行时切换；
- `switchPresetToInner` 的短路判据需同时比较 preset **与** 模型——否则「同一 persona、换模型」会被短路吞掉；
- 模型是 dsh 会话的初始化参数，变更需一次重启（与既有 `switchModelTo` 同路径）。

**验收**：
1. 岗位设模型 A 起回合 → 该回合的 usage/账单记录显示模型 A；
2. 同岗位改模型为 B → 下一回合以 B 运行（允许一次重启，不得静默沿用 A）；
3. 未设模型的岗位 → 与现状一致；
4. a2a 岗位不受影响（其模型归该服务的部署配置）。

### F3 · 计费归属与可见性

岗位回合的模型 SHALL 记入该回合的用量/账单事实（沿用既有 turn 记录，不新造账本）。舰队控制台的岗位卡片 MAY 展示当前模型。

**验收**：按模型聚合某舰队近 24h 的用量可出数；未设模型的岗位归到「默认」档。

### F4 · 舰队默认、岗位兜底与解析链（定案）

**F4.1 舰队默认模型**：`fleets` SHALL 增加可空列 `default_model`，由舰队设置面读写。它身兼两职：未声明模型的岗位跑什么 + 模型失效时落哪。SHALL NOT 拆成两个旋钮。

**F4.2 岗位兜底模型**：`fleet_members` SHALL 增加可空列 `fallback_model`，仅在 `model` 非空时可设。清空 `model` 时若兜底仍挂着 SHALL 400 并提示先清兜底（杀链序歧义）。

**F4.3 解析链**：每个 persona 成员回合开始时，运行时模型 SHALL 归位到解析链的第一个 roster 命中档：

```
[ member.model, member.fallback_model, fleet.default_model, cell 默认 ]
  去空、去重
  声明过但 roster 不含  -> 落下一档 + 留痕（指名哪一档失效）
  未声明(null)          -> 安静跳过
  全灭 / roster 为空    -> 回合如实失败
```

岗位表现查、不物化进任务记录（改模型零对账）。归位在 preset 归位之后执行，复用既有 `switchModelTo`（含短路、流守卫、live `model/set`）。

**F4.4 明拒口径**（写面一律 400，指名原因）：

| 情形 | 处置 |
|---|---|
| a2a 岗位设 `model`/`fallback_model` | 400——a2a 的模型归该服务的部署配置 |
| 集群（cluster）岗位设模型 | 400——集群岗归各成员岗位 |
| `fallback_model` 非空而 `model` 为空 | 400——链序歧义 |
| 清空 `model` 时 `fallback_model` 仍挂着 | 400——先清兜底 |
| 标识符形状非法 | 400（`MODEL_ID_RE`） |
| 不在宿主 runner 的 roster | 400（`modelChoiceError` 的 roster 闸） |
| 不在计费车道 | 400；cell 侧暂无车道面时降级开放（`lanes=null`） |

**F4.5 兜底只发生在选型时**：roster 查不到 = 选型失效 → 落下一档、回合照跑。模型在 roster 里但执行中炸（provider 5xx / 超时 / 限流 / 额度耗尽）SHALL 保持现状语义：回合失败，等下个 cadence 自然重试。**执行中换模型重跑不在本期**（会踩进「重试几次、失败回合算谁的」的自动降级泥潭）。

**F4.6 可见性**：`memberView` 返回 `model` / `fallbackModel` 原值，并给出 advisory 的 `effectiveModel`（读时按链 + 实时 roster 计算）；舰队控制台岗位卡片展示生效模型与「声明已失效、正落兜底」状态。usage 记**实际运行**的模型，按模型聚合天然成立。

### F5 · 岗位间唤醒（横向派发）—— 阻断「流水线」类舰队

**问题**：舰队成员回合只暴露两个工具——`report_status` 与 `escalate`。**没有任何工具能让一个岗位唤醒同舰队的另一个岗位**。被动岗的唤醒路径只有两条：cron 到点、或主人批复拍板后的 `execute-now`。

**后果**：任何「上游发现 → 下游执行」的流水线型舰队都不成立。以柏讯内容舰队为例：巡线岗发现覆盖缺口，只能 `escalate` 给主人等人批；内容岗永远等不到活。组织层级只承载**上行**信息流，**下行只有到人，不到岗**。

**要求**：新增成员工具 `dispatch`，允许成员岗位唤醒**同舰队**的另一个岗位。

- 入参：`{ targetRef, brief, evidence?, urgency? }`——brief 为自包含任务说明，evidence 为触发依据（引用可解析，不要求目标岗重查）。
- 约束：
  - 仅同 `fleetId` 内派发，跨舰队一律拒绝；
  - 不得自派（防自激循环）；
  - 派发深度 ≤1（被派发回合不得再派发），防止链式放大；
  - 每岗位每回合派发次数上限 + 舰队级每小时上限，超限结构化拒绝；
  - 目标岗为 a2a 时走既有委托通道，不新开路径。
- 语义：被派发回合是一次**正常成员回合**——preset 归位、模型归位、身份注入、报数通道全部照常；prompt 前缀携带派发者身份与 brief（与舰队身份行同构，不另造模板机制）。
- 留痕：派发落一条事件（派发者、目标、brief 摘要、回合 id），驾驶舱可见。

**验收**：
1. 巡线岗派发内容岗 → 内容岗起回合，prompt 含派发者身份与 brief；
2. 自派、跨舰队、超深度、超频次 → 结构化拒绝并指名原因；
3. 目标岗为被动岗时派发仍可唤醒（这正是被动岗的存在意义）；
4. 未使用 dispatch 的既有舰队行为逐字节不变。

**明确不做**：不做多跳编排（A→B→C）、不做共享黑板/消息总线、不做 agent 自由对话。派发是**单向、一次、有界**的唤醒，不是通信平台。

**替代方案（不推荐）**：让下游岗自带 cron 主动轮询上游报数。可行但把「事件驱动」降级成「轮询」，且下游要能读上游报数——目前 `{{fleetContext}}` 只注入**直接下级**给上级，下级读不到上级。要支持得反过来加一条下行读通道，改动面更大且语义更绕。



这条必须说清楚，否则会误以为舰队能整夜自愈：

| 「失效」的形态 | 本期是否自动处理 | 机制 |
|---|---|---|
| **模型下架 / roster 漂移**（模型从可用清单消失） | ✅ 自动 | 解析链落下一档 + 留痕 |
| **执行中故障**（5xx / 超时 / 限流 / 额度耗尽 / 免费额度用光） | ❌ 不自动 | 回合失败，下个 cadence 重试；届时若该模型仍在 roster，会再次选中它并再次失败 |

**后果**：若某个免费模型额度耗尽但仍在 roster 里，舰队会**每个 cadence 都失败一次**，而不是换模型。要真正「额度用光就换」，需要额外一条规则：连续 N 次失败 → 把该模型标为不可用并落兜底。这属于本期之外，见 F4.7。

**F4.7（建议，未纳入本期）连续失败降档**：同一岗位连续 N 次因模型侧错误失败时，SHALL 把该模型临时标记为不可用（带冷却窗），解析链跳过它；冷却窗结束或人工清除后恢复。需要先定义「模型侧错误」的判别口径（区分模型问题与业务/工具问题），否则会把正常失败误判成模型失效。

## 五、明确不做

- **不放开 pack persona 的 `model` 禁令**（包作者的边界不变）；
- 不引入「岗位级 provider/凭据」——只有模型选择；
- 不做跨岗位的模型编排/自动降级（成本超限自动换便宜模型）——属后续增量；
- 不改 `refresh-scheduler`、mission 四元组与 serving-plan 语义。

## 六、与柏讯侧的关系

柏讯的「柏讯 · 产物构建」包内嵌 3 岗舰队模板（巡线/内容/源）。模板本身**不需要**声明模型——模型是部署者的运行决策，不是包作者的内容决策。因此本需求落地后，柏讯侧**零改动**即可用：装包 → 建队 → 在岗位编辑里给巡线岗挑便宜模型。

若 F1 采用「岗位模型写在模板里」的另一种设计，请告知——那样柏讯侧需要在模板里加字段，并且会重新打开「包作者能否影响运行时成本」这个已经关掉的问题。**建议不这么做。**

## 七、参考落点

```
server/routes/fleets-db.js      fleet_members 表 + createMember/updateMember
server/routes/fleets.js         POST/PATCH members、memberView、targetResolver
server/cron-runner.js:220       runPersonaTurn（成员回合）
server/agent-session.js:862     switchPresetToInner（preset 短路与重启）
lib/agent-serving.js:374        modelChoiceError（三闸，直接复用）
```
