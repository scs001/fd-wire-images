# 壹座/萬星侧需求：客户域动作的舰队消费（wire-domain-actions 跨线件）

> 提出方：fd-wire（柏讯线）
> 日期：2026-10-29
> 关联：fd-wire change `wire-domain-actions`（已立项，strict 绿，待实施）；
> `paas-wire-content-fleet-requirements.md` §三 P1（岗位身份注入——**已更正：见 R1**）
> 状态：**已评审（2026-10-29，paas 侧回执见 §六；总裁决：R1 更正/R2 采纳/R3 采纳+对偶义务/R4 收讫）**

---

## 一、背景（一段话）

柏讯动作闸正在从「平台自理动作」扩为「客户域动作类型」：客户在自己的实体上声明
业务动作（批准、标记、指派、状态流转），执行落修正层（overlay），全链
propose→confirm→execute→audit。对外通道是 wire-ops-mcp 三个新工具
（`wire_action_list` / `wire_action_propose` / `wire_action_status`）——
舰队岗位将可以提议域动作，owner 在柏讯控制台确认。这使「舰队巡检发现问题 →
提议业务动作 → 人拍板 → 数据落账」成为完整闭环，是柏讯从 BI 工具走向
运营平台的分水岭。

## 二、已核实：不需要 paas 改动的部分

| 事项 | 证据 | 结论 |
|---|---|---|
| pack 引用 wire-ops-mcp 新工具 | allowTools 机制按叶名声明，新工具自动可声明 | 现成；仅 fleet-packs 模板要改（柏讯仓内，非 paas） |
| 壹座动作闸与柏讯动作闸并存 | 两闸本就分层（平台动作 vs 数据域动作），互不越界 | 不需要合并或代理 |
| MCP 工具面新增 | wire-ops-mcp 走 registry 网关注册，柏讯侧自行完成 | 无需 paas 改动 |

> **2026-10-29 评审补充**：上表之外仍有**两项批次内 paas 侧工作**，不排进同批
> 柏讯的 pack 发布会撞门（详见文末 §六 评审回执）。

## 三、需求

### R1 · 岗位身份注入（前置，沿既有需求，升级为域动作前置）

> **⚠️ 2026-10-29 评审更正**：本节「至今未落地」的前提**已过时**——身份注入已于
> 2026-10-23 上产（注入点 `server/cron-runner.js` runPersonaTurn，prompt 首部
> 【集群岗位身份】行；渲染 `server/fleet-reports.js:138-170`；主 spec
> `agent-fleet-report-lane/spec.md:96`；台账 `docs/registry-maintenance.md` §15.16）。
> 在产 key 名为 `fleet=` / `post=` / `title=` / `target=<kind>:<id>`（spec 示例的
> `ref=` 已由 paas 校正）。**唯一真实缺口 = 回合腿**：身份行未带
> `roundChildSessionId`，agent 无法自证回合——paas 已立案 change
> `add-fleet-identity-turn`（validate 绿待实施，约两行），上产后身份行追加
> `turn=<回合标识>`。**柏讯侧落法**：`wire_action_propose` 的来源标注原样透传
> （不解析），技能稿按在产 key 名拼装并预留 `turn=` 字段位。

`paas-wire-content-fleet-requirements.md` §三 P1（R1-a 占位符 / R1-b 成员环境变量）
至今未落地。域动作的审计台账要区分「哪个岗位提议的」——MCP 提议目前只能归到
凭据身份，到不了岗位。

**要求**：成员回合内岗位身份（fleetId / post ref / memberId）对 agent 可见，
使 `wire_action_propose` 可携带 `source_ref` 形参（部署/岗位/回合三元组，
柏讯侧将作为台账可选标注字段）。

**验收**：真实成员回合提议域动作，台账实例的来源标注可读到岗位三元组。

### R2 · 拍板面裁决：唯一在柏讯控制台（建议，需 paas 确认不反对）

舰队提议的域动作，确认动作发生在**柏讯控制台**（动作闸确认卡）。壹座侧
**不建第二确认面**——建议壹座在舰队观测面把该提议呈现为
「已提议 → 待柏讯 owner 确认」状态 + 深链跳转（新标签页、不带任何会话凭据参数，
沿 console-ai-entry「去壹座」反向同款纪律）。

**否决的替代**：壹座内确认后再回传柏讯执行——双闸双确认（owner 要点两次）、
审计两线漂移、与「写操作永不进工具面直调」的结构性保证冲突。

> **2026-10-29 评审结论**：**采纳，附结构实证**——壹座动作闸（`lib/action-registry.js`）
> 只包已注册动作（现役 10 条 wire 动作 id），`wire_action_*` 不注册进壹座注册表
> 就不会进壹座确认卡（与 `wire_artifact_dashboard_create` 先例同形），「两闸分层、
> 互不越界」核实成立。观测面：驾驶舱回合 trace 天然呈现 propose 调用与 pending
> 结果；「待柏讯确认」持久徽标本期不做（下轮巡检读状态即可达意）。
> **共同护栏**：wire_action 提议**永不镜像进壹座拍板 lane（decision items）**——
> 否则单一拍板面从后门反转成双确认。此护栏已写入 `wire-domain-actions` spec。

**验收**：舰队观测面能看到提议状态与深链；柏讯确认后状态回流为已执行
（读 `wire_action_status` 即可，无需推送通道）。

### R3 · 舰队编排语义：提议不阻塞回合

`wire_action_propose` 立即返回 pending（不等待确认）。舰队岗位的回合编排
SHALL 把「提议已提交」计为该步完成，不得轮询等待确认结果（确认是人的节奏，
可能数小时）。下一轮巡检经 `wire_action_status` 读取结果即可。

**验收**：提议后回合正常收尾；下轮回合能读到前轮提议的执行结果。

> **2026-10-29 评审结论**：**采纳，附对偶义务**——壹座回合收尾与「提议即返回
> pending」天然相容；对偶义务在柏讯侧：`wire_action_propose` 的即时性 SHALL 写进
> spec（已写入 `wire-domain-actions`），若未来实现变成阻塞等确认，壹座侧会如实
> 把回合归因 stalled（既有归因面），不会静默假绿。

### R4 · 计量与凭据轨（知会，无 paas 改动）

域动作执行落 overlay 写，不产生行数读取计量（柏讯侧口径）。提议/状态查询走
调用方凭据（舰队线池凭据，沿双凭据归属）。壹座侧无需改计量。

> **2026-10-29 评审结论**：知会收讫，零改动。

## 四、边界

- 本文全部需求落地前，柏讯侧 change 独立可用（控制台人用通道先通，agent 通道
  随 R1 生效后增强归因）。
- 若 R2 裁决反转为「壹座内确认」，才产生 paas 开发项——**不建议**。
- fleet-packs/wire 模板的 allowTools 增列 `wire_action_*` 属柏讯仓改动，
  随 wire-domain-actions 实施同批做，不在本文范围。

## 五、评审回执（2026-10-29，paas 侧）

总裁决：R1 前提更正（身份注入 2026-10-23 在产，唯一缺口=回合腿，paas change
`add-fleet-identity-turn` 补齐）/ R2 采纳（壹座动作闸只包注册动作的结构实证 +
镜像禁令护栏）/ R3 采纳+对偶义务 / R4 收讫。

**批次内 paas 侧两项工作（须与柏讯部署同批排程）：**

1. **registry 工具面快照重烘焙**：`/wire-ops-mcp` 条目快照现停 13 工具；该服务
   AuthGate 匿名 401，发布门 live probe 探不到（「unresolved 而非 absent」，
   台账 §1151/§1616 有实录与 Mongo 直写配方）。**柏讯部署三新工具后、
   fleet-packs 模板把 `wire_action_*` 写进 allowTools 之前**，壹座须按既有配方
   重烘焙快照（对象数组 + num_tools 同步），否则 pack 发布被拒。→ 柏讯部署
   完成即通知壹座，同批排程。
2. **wire-tool-risk 快照刷新触发 CI 归账 + 标签契约**：`lib/wire-tool-risk.js`
   从 fd-wire tool-surface.json 的 `tool_risk` 生成，两处 CI 硬断言工具全集恰为
   16（新工具入快照后由壹座归账 16→N；`wire_action_propose` 进入 gated write
   全集、声明它的 pack 不再够格当 copilot 聚焦面——期望行为，柏讯勿惊）。
   **标签契约：三新工具只复用既有 risk 值，勿新增**——拟定
   `wire_action_list`/`wire_action_status` = `read`（不入写全集）、
   `wire_action_propose` = `write`（入全集、不注册壹座动作），随本回执知会壹座。

**对柏讯侧的四项请求与落实：**

| # | 请求 | 落实 |
|---|---|---|
| 1 | R1 验收以在产身份行格式为准（`fleet=`/`post=`/`title=`/`target=`）；技能稿/台账解析留 `turn=` 字段位 | `wire_action_propose` 增可选 `source_ref` 形参**原样透传**（wire 不解析），技能稿按在产 key 名拼装并预留 `turn=` |
| 2 | tool_risk 标签实施前知会壹座 | 拟定标签随本回执知会（list/status=read、propose=write），实施时写入 tool-surface.json |
| 3 | propose 即时返回 pending 写进 spec 场景 | 已写入 `wire-domain-actions`（MCP 提议通道需求，含对偶义务措辞） |
| 4 | 部署后通知壹座重烘焙 registry 快照，再走 pack 发布 | 列入 `wire-domain-actions` 收口任务（部署完成即通知） |

壹座侧证据路径（复核用）：`server/cron-runner.js`（runPersonaTurn 身份行装配）、
`server/fleet-reports.js:138-170`（renderFleetIdentity/ghost-post 守卫）、
`openspec/specs/agent-fleet-report-lane/spec.md:96`（在产主 spec）、
`docs/registry-maintenance.md` §15.16（2026-10-23 发布+真机验收）、
`openspec/changes/add-fleet-identity-turn/`（回合腿 change，validate 绿待实施）、
`lib/action-registry.js`（R2 结构实证）、`lib/publish-tool-gate.js`（§五①依据）、
`lib/wire-tool-risk.js` + `scripts/test-wire-tool-risk.mjs`（§五②依据）。

---

## 六、交付完成通知（柏讯 → 壹座，2026-10-30）

> **⚠️ 2026-10-30 更正（柏讯自复核）**：本节初版的「请壹座执行：重烘焙快照」
> **前提已过时**——壹座已于 2026-10-29 完成，`/wire-ops-mcp` 条目**双名单都已就位**
> （活栈 cheap-8 k8s 实查：`num_tools=19` / `tool_list` 19 / `metadata.surface.commercial_tools`
> 19，`updated_at=2026-10-29T11:33:32Z`；商业名单与仓内
> `scripts/export_surface_commercial.py --server wire-ops-mcp` 推导结果**逐元素一致**）。
> **pack 现在即可发布，不需要再动 registry。** 详见 §六补记。
>
> 另一处更正：初版只说「重烘焙工具面快照」，**漏了第二条腿**。registry 快照有两条独立
> 通道，服务自报只覆盖其一（详见 §六补记 A）。

**三新工具已上产。**（初版此处为「请按 §五 第 4 项重烘焙 registry 工具面快照」——已完成，见上）

### 已交付（柏讯侧全部完成）

| 事项 | 证据 |
|---|---|
| wire-ops-mcp 部署含三新工具 | 生产 `wire-ops-mcp:sha-d8a81b6`；pod 内 FastMCP 枚举 **19 个工具**，含 `wire_action_list` / `wire_action_propose` / `wire_action_status` |
| 工具面自报 registry | `/health` → `{"status":"ok","last_surface_report":"ok"}`（add-self-reported-tool-surface 之后的自报通道，**不再需要 Mongo 直写**） |
| tool_risk 标签（复用既有值，未新增） | `tool-surface.json`：`wire_action_propose = "write"`；list/status 未列入 `tool_risk` → 按既有约定即 `read`（不入写全集）。**全程只用 read/write/cost 三值，无新值** |
| 迁移 0043 上产 | 生产双库 `alembic_version = 0044_dataset_source_config`；`domain_action_types` / `_type_events` / `_instances` 三表在位，`ai_action_proposals.conversation_id` 已转 nullable |
| 真库端到端 | 类型声明 201 → 提议即时回执 `pending_confirm`（tier=inline，不阻塞回合）→ 实例台账 200（`source_ref` 身份行原样落库）→ 列表 200；残留已清理 |
| propose 永不镜像进拍板 lane | 保持（用户红线）：提议只落柏讯域动作台账，不进壹座的 decision items |

### 请壹座执行（只有一项）

**重烘焙 registry 工具面快照**——执行既有配方
（`docs/registry-maintenance.md` §79 自报通道 / `scripts/refresh-wire-tool-surface.mjs`），
让 `lib/wire-tool-risk.js` 从 fd-wire 的 `tool-surface.json` 取到本次新增键。

> **2026-10-30 更正**：以上「请壹座执行」项**已完成**（2026-10-29）。下面留作复核口径，
> 不再是需要壹座动作的事项。**真正的常驻约束见 §六补记 A。**

预期观测（复核口径）：

- CI 硬断言的工具全集计数 **16 → 17**（`wire_action_propose` 进入 gated write 全集）；
- 声明 `wire_action_propose` 的 pack 不再够格当 copilot 聚焦面——**期望行为，柏讯勿惊**；
- `wire_action_list` / `wire_action_status` 计为 read，不进写全集。

**2026-10-30 柏讯实测复核**（`paas@da00471f`，`lib/wire-tool-risk.js`）：

```
WIRE_TOOL_RISK 总数            = 17        ← 断言对象，与 16→17 口径一致
WIRE_WRITE_TOOLS（全集键集）    = 17        ← 含 cost 一条：wire_market_subscribe
  其中 risk = "write"          = 16
  其中 risk = "cost"           = 1
wire_action_propose            = { risk: "write", server: "wire-ops-mcp" }
isWireWriteTool("wire_action_list")   → false   ← list/status 按约定在全集外
isWireWriteTool("wire_action_status") → false
paas 动作注册表内 wire_action* 条目   → 0       ← 故意缺口（R2 单一确认面），已钉进断言
```

四条相关 CI 测试实跑全绿：`test-wire-tool-risk` 6/6、`test-publish-tool-gate` 15/15、
`test-official-packs-lint` 6/6，合计 37/37。**「16 → 17」是全集键集口径；按
`risk === "write"` 过滤得到的是 16（17 减去唯一一条 cost）。两数不矛盾，勿混用。**

### 时序约束（已满足）

`fleet-packs` 模板把 `wire_action_*` 写进 `allowTools` 之前，壹座侧须先完成重烘焙
（网关 registry 快照落后于真实工具面时，装包会以「工具不存在」失败）。

**2026-10-30 状态：前置已满足**——活栈条目 `tool_list` 19 / `commercial_tools` 19，
两条腿都在位。**柏讯这边可以发 pack 了，不需要再等任何回执。**

---

## 六补记 · registry 快照有两条腿，自报只覆盖一条（2026-10-30 柏讯实测）

### A. 双通道结构（这是初版漏掉的部分）

registry 里 `/wire-ops-mcp` 的工具面由**两条独立数据**共同决定，发布门两处都会读：

| 腿 | 字段 | 更新方式 | 自报覆盖 | 实测现状 |
|---|---|---|---|---|
| ① 存在性名单 | `tool_list` + `num_tools` | 服务启动自报（push，`add-self-reported-tool-surface` 之后） | ✅ **是** | 19 / 19 |
| ② 商业闭包名单 | `metadata.surface.commercial_tools`（名字数组）+ `tools_commercial` | 从仓内 `tool-surface.json` 推导后写入（`scripts/export_surface_commercial.py`） | ❌ **否** | 19 / 19 |

**关键后果**：发布门 `lib/publish-tool-gate.js:165` **先判商业闭包，再判存在性**。
若只做了自报（腿①），腿②仍停在旧名单，那么声明新工具的 pack 会**在闭包处被拒**，
而错误文案是：

```
MCP server '<path>' is surface-registered; tools outside its commercial set may not enter a pack: <names>
```

——完全不提「重烘焙/重导出」，极易被误判成「工具没接线」。这就是初版「重启一下就好」
会让 pack 发不出去的机制。

### B. 常驻 runbook：每次工具面变更，两条腿都要走

**触发条件**：任何 MCP 服务新增/删除/改名工具，且该服务已上牌进商业供给面。

```
① 服务侧   启动自报     → 自动更新 tool_list + num_tools（重启即生效，无需人工）
② registry 重导商业名单 → 必须人工/脚本触发，自报不会碰它
```

第②步的规范做法（单一真源 = 仓内 manifest，禁止手写名字）：

```bash
cd /Users/chengsishi/finddata

# 1) 从 manifest 推导商业名单（只读，先看差什么）
python3 scripts/export_surface_commercial.py --server wire-ops-mcp

# 2) 有差异才写回 registry 条目的 metadata.surface
#    （写入端脚本 + 活栈位置见 paas docs/registry-maintenance.md §79 / MCP-REGISTRY.md）
```

**校验（发布前必做）**：拿活栈条目与推导结果做逐元素比对，必须 `IDENTICAL: true`。
柏讯本次实测的取数口径（活栈 = cheap-8 k8s `ns mcp-registry`，**不是** cheap-1 那条已冻结的
compose 栈——两栈 Mongo 各自独立，查错栈会看到 10-15 的 13 工具陈迹）：

```bash
# 活栈 registry pod 内，用 pod env 里的 DOCUMENTDB_* 直连（mongosh 双层 exec 传密码易认证失败）
kubectl --context cheap -n mcp-registry exec -i <registry-pod> -- /app/.venv/bin/python -c '
import os, json
from pymongo import MongoClient
uri="mongodb://%s:%s@127.0.0.1:27017/mcp_registry?authSource=admin" % (
    os.environ["DOCUMENTDB_USERNAME"], os.environ["DOCUMENTDB_PASSWORD"])
d=MongoClient(uri,serverSelectionTimeoutMS=8000)["mcp_registry"].mcp_servers_default.find_one({"server_name":"wire-ops-mcp"})
print("num_tools   =", d.get("num_tools"))
print("tool_list   =", len(d.get("tool_list") or []))
print("commercial  =", len((d.get("metadata") or {}).get("surface",{}).get("commercial_tools") or []))
print("updated_at  =", d.get("updated_at"))
'
```

### C. 本次终验证据（真条目 → 真门，非模拟）

从活栈 Mongo 取 `/wire-ops-mcp` 条目原文（39 字段全量），按 `registry-bridge.js:234`
`registryServerIndex` 的同款映射构造 `serverInfo`，喂真实 `toolGateErrors`：

| 用例（`mcpServers` 为数组、每项带 `registryName` + `allowTools`） | 结果 |
|---|---|
| 正控：`wire_action_list` + `propose` + `status` + `wire_dataset_query` + `wire_demand_submit` | **0 errors** ✓ 放行 |
| 负控：名单外 `wire_dataset_correct` | **1 error**「tools outside its commercial set」✓ 门未被放开过头 |
| 正控2：三 action 工具全量 | **0 errors** ✓ |

> **踩坑留痕**：柏讯首次跑负控得 0 errors，原因是把 manifest 写成 `mcpServers: {name: {tools:[...]}}`。
> 真实结构是 **数组 + `registryName` + `allowTools`**（`declaredToolsByServer` 只认这两个键，
> `lib/publish-tool-gate.js:33`）。形状错的 manifest 会被静默判成「无声明」，负控假绿。

### D. 需求变更后的固定动作清单（写进 pack 发布 SOP）

1. 服务加/删工具 → 出片滚版（腿① 自报自动生效）；
2. **通知壹座重导出商业名单**（腿②，自报不覆盖）——漏这步会在闭包处被拒且报错不指向真因；
3. 用 §B 的校验片段确认 `tool_list` 与 `commercial_tools` **都**对上 manifest；
4. 再改 `fleet-packs` 模板的 `allowTools`，然后发 pack。
