# 萬星/壹座侧需求：Agent 集群自愈执行段（fleet-self-heal-execution）

> 来源：finddata 工厂集群 change（`openspec/changes/add-infra-factory-fleet`，2026-10-31 explore 定案）
> 对象：paas（壹座 + 萬星）仓
> 状态：**需求源，待 paas 会话立项**。本文是需求源，paas 的 proposal 引用它；实施细节以 paas 的 design 为准，两处冲突时以本文口径为准并回写。
> 与 `paas-fleet-orchestration-modes.md`（回合契约抽象）、`paas-fleet-post-model-requirements.md`（岗位级模型）独立可分排，但 E1/E2 与 orchestration-modes 第①步（回合失败一等化）同一地基——**先实读现状，orchestration-modes 文中"失败未一等化"的结论已被 `fix-fleet-failure-visibility` 推翻**，见二。

## 一、问题（一句话）

平台已有完整的"发现→提案→人工批准"面（failureStreak 读态、fleet-iteration 状态机、decision_queue、action-gate 审批），但**批准之后的执行段缺三样硬东西**：失败计数不触发停手（熔断）、批准人只能是自然人（自动窗口无法存在）、执行后无动作级回滚——没有这三样，"人类职责转为集群主人与迭代者"的北极星就是假的：主人还得逐单批。

## 二、现状核查（已实读 paas 代码，2026-10-31）

| 层 | 现状 | 证据 |
|---|---|---|
| 回合失败 | **已一等化**：turn 流产 `failureStreak/failureSince/lastFailureKind`，`ERROR_KINDS` 闭集七类（runtime_not_ready/timeout/tool_permission/model_upstream/business_refused/context_budget/other） | `server/fleet-events.js:21-29`；`server/fleet-reconcile.js:229`（fix-fleet-failure-visibility D3 注释） |
| 连续失败 | 默认阈值 `FAILING_STREAK_DEFAULT = 3`，读态 precedence：vacant→passive→paused→missing→**failing**→on_duty；interrupted 轮不计败、一次成功清零 | `server/fleet-reconcile.js:229,240,360-372` |
| **关键缺口** | failing 只是**读态**（控制台不再假绿），但任务引擎照常排该岗位下一轮——计数没有触发任何停手 | reconcile 派生在读路径（"read-time, never stored"），全仓无调度侧消费 failureStreak 停排的调用点 |
| 迭代提案状态机 | `ITERATION_STATES = [proposed, approved, casting, publishing, published, failed]`；failed 带 `axis`（fleet-missing/source-template-missing/validate/publish/unhandled），**无 rolled_back 态、无回滚动作** | `server/fleet-iteration.js:17,69-119` |
| decision 状态机 | `DECISION_STATES = [open, approved, rejected, expired, delivered]`；approve 回调进铸造链（"the decision is the gate"） | `server/fleet-decisions.js:12,21-23`；`server/fleet-iteration.js:62-63` |
| 动作闸 | `lib/action-registry.js`：集中声明 id/line/tool/risk；risk→确认 tier 映射；**未注册动作 copilot 不得执行**；提案状态 pending→approved→executed，digest 覆盖用户值 | `lib/action-registry.js:3,68-69,96-169`；`server/routes/actions.js:53,120,169-208` |
| kill switch 现状 | 岗位级 `cron_pause` 存在（cron-mcp 五工具）；**无舰队级一键停全舰、无平台级总闸**，也无"自动动作已停摆"的可读数位 | `server/cron-mcp.js:72-166` |
| 互斥与预算 | 无同-target 互斥锁（并发双提案可命中同 workload——10-28 坏镜像并发滚产事故形状）；无每回合动作预算 | 全仓 grep 无 mutex/budget 相关执行面 |

**结论**：需求不是"造失败可见性"（已有），而是**把可见性接成治理**——计数→停手、批准→可机器、执行→可回滚。

## 三、需求

### E1 · 熔断：连续失败 SHALL 触发停手，不只触发读态

岗位 `failureStreak` 达到 `failingThreshold()` 时，任务引擎 SHALL 暂停该岗位排程（复用 reconcile 已有阈值与计数，**不另立第二把尺**）；暂停 SHALL 留痕（kind=breaker，带 lastErrorKind）且 SHALL 出现在岗位读面上（failing 与 breaker-paused 区分）。恢复 SHALL 限于：一轮成功自动回排（沿用"一次成功清零"语义），或 Owner 显式 re-enable。`interrupted` 轮 SHALL NOT 计入熔断触发。

#### 场景：十二连败真停手

- WHEN 某岗位连续 3 个回合以 timeout 失败
- THEN 第 3 次失败后任务引擎不再排该岗位，岗位读面显示 breaker-paused 且点名 streak=3 与 lastErrorKind；此前十二连败照常排程的形态 SHALL NOT 再现

### E2 · kill switch：舰队级与平台级一键停 SHALL 存在且可读数

SHALL 提供**一键暂停整支集群全部岗位排程**（含 paused 标记与恢复入口），并 SHALL 提供**平台级自动动作总闸**（关闭后：政策引擎全部 pending 提案回落人工批准，已排自动动作停发）。两档开关的状态 SHALL 在 Owner 经营面可读。本项落地前，工厂集群验收判据④（kill switch 演练）SHALL NOT 判 PASS。

### E3 · 自动窗口政策引擎：批准人可以是政策，仅限闭集白名单

action-registry 的 pending 提案 SHALL 增加"政策批准人"通道：命中以下**四重前置**才 pending→approved（approver="policy" 留痕），任一不满足即回落人工批准——

1. **动作在自动窗口白名单**（闭集三类，与 finddata `infra-factory-fleet` spec D3 逐字一致）：重启指定 deployment / 回滚 image tag 至上一版 / 暂停异常 cron；注册进 `lib/action-registry.js`，tool+risk 照 wire-tool-risk 钉死。
2. **红线永不自动**（五类永远人工）：新建部署、DB 迁移、权限/凭据变更、跨服务批量、删除类——注册面 SHALL NOT 为其提供 policy approver 路径。
3. 熔断未触发（E1）且 kill switch 未开闸（E2）。
4. 同-target 互斥锁空闲 + 本回合动作预算未超（E5）。

#### 场景：白名单自动批准

- WHEN 一条"重启 deployment Y"提案命中白名单且四重前置全过
- THEN 提案状态 pending→(approved, approver=policy)→executed，审计两表可查；无需第二次人工确认

#### 场景：总闸一开全回落

- WHEN 平台级自动动作总闸关闭
- THEN 后续白名单提案一律停在待人工批准，已排队自动动作停发，开关状态 Owner 可读

### E4 · 动作级回滚：rolled_back SHALL 是一等状态

`ITERATION_STATES` 与动作提案状态 SHALL 各增 `rolled_back`（failed 带 axis 的形态保留）。执行段每次成功执行 SHALL 记录**回滚对**（白名单三动作的反向操作即白名单自身：重启的回滚=再重启或不动、tag 回滚的回滚=恢复原 tag、暂停的回滚=恢复排程）；自动执行后 SHALL 进**观察窗**（复用 as-of/freshness 语义），观察窗内该 target 再次异常 SHALL 优先触发回滚对而非新动作。

### E5 · 同-target 互斥 + 每回合动作预算

同一 target（deployment/workload/cron 粒度）的自动动作 SHALL 互斥——两提案命中同 target 时一执行一排队，SHALL NOT 并发对同 workload 动手（10-28 平台事故形状）。每回合自动动作数 SHALL 有预算上限，超限即触发停手并 escalate（与 E1 熔断同源留痕）。

### E6 · 审计：批准人字段区分 human/policy，复用既有两表

动作提案审计 SHALL 新增 `approver ∈ {human, policy}`（policy 行附带命中的白名单项与四重前置判定结果），既有两表与 decision 先入痕语义不动。

## 四、与 finddata 线的分工边界

- **paas 承接治理**：E1–E6 全部落在 paas（reconcile/cron 引擎/action-registry/状态机/审计）——"批准即自动跑"是平台能力，不是 finddata 私件。
- **finddata 承接手脚**：infra 写面执行体（deployment restart / tag 回滚 / cron pause 对 K8s 的实际触达）注册为独立在册 MCP server（`infra-operator` 候选名，源码零口令、凭据只在部署环境、只暴露白名单三工具+其反向对）；读面走 `infra-telemetry` 只读 server。**paas SHALL NOT 代持 kubeconfig**——政策引擎判完，执行调用点指向在册 server 名。
- 工厂集群（finddata `add-infra-factory-fleet`）是 E1–E6 落地的**第一个验收客户**：首轮判据四条 + 3 档接线演练（tasks.md 组 5）即本需求稿的收口验收。

## 五、验收（可计数，缺一不销账）

1. E1：注入 3 连败 → 岗位自动停排 + 读态可辨（一条真机记录）。
2. E2：一键停全舰演练生效 + 总闸关闭后白名单提案全部回落人工（两条演练记录）。
3. E3：白名单提案 policy-approved→executed 全链留痕 ≥1 条；红线五类各验一条——政策路径不可达（5 条拒绝记录）。
4. E4：一次 tag 回滚 + 观察窗 + 触发回滚对（或确认回滚对可用）≥1 例。
5. E5：并发双提案同 target → 一执行一排队，零并发写（1 例）；预算超限停手 escalate（1 例）。
6. E6：审计查询 approver=policy 行含判定明细（1 例导出）。

## 六、Non-Goals

- 不新建第二套调度器/执行路径（与 orchestration-modes 基调一致；E1/E2 落既有 reconcile+cron 引擎，E3 落既有 action-gate）。
- 不给工厂集群或任何 pack 直 kubectl 写权（写面=GitOps/在册 server 单一通道）。
- 不在本需求稿内实现——paas 姊妹 change 立项后以 paas design 为准施工。
