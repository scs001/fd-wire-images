# 壹座侧需求：wire 线 pack 安装闸分支 + fleet 行更新审计 + 凭据首连交付 + consent 页修复 + 回合启动期失败可见性

> 来源：fd-wire change `wire-content-fleet` 交接执行（2026-10-24 下午–晚，`openspec/changes/wire-content-fleet/reports/2026-10-24-handoff-execution.md`）；§五、§六 为 2026-10-26 归档前核验补充
> 对象：paas（壹座）仓。六件按优先级排列，P0 阻断真实客户装 wire pack。
> wire 侧配合事项在文末（都是已做或零代码）。

## 一、P0 · pack 安装闸缺 wire-customer/lex 分支（阻断客户车道）

### 问题（一段话 + 实测证据）

wire 线服务器的 pack 安装闸把「wire-customer」凭据 ref 的连通性检查**fall-through 到 registry 凭据**——没有 registry（市场）凭据的真实客户**装不了任何 wire pack**，报错文案还误导成「connect the MCP market first」。

实测：wire-tester2（basic 档）装「柏讯·产物构建」v2，pack-store 安装闸 409 `credential-required`；同期 `GET /api/lines/wire-customer/connection` 显示 `connected:true`（凭据明明活着）。给该测试号塞一条占位 registry 凭据行后**立即放行**——证明闸查错了凭据。运营号 platform-test 此前能装，只是因为其恰有一条 sso 来源的 registry 凭据（2026-10-23）。

### 根因（代码）

`credential-refs.js`：

- `credentialRefForEntry(entry)` 正确地把 wire 线服务器（WIRE_LINE_SERVERS）派发到 `lineCredentials.WIRE_CUSTOMER_CREDENTIAL_REF`；
- 但 `credentialConnected(refName, email)` 只显式处理 facet-market / chatwoot / paper-trading 三种 ref，**其余一律 `return registryCredentials.status(email).connected`** ——wire-customer / lex 两条线凭据 ref 落进 fall-through；
- `credentialMissingReason(refName)` 同样 fall-through，产出误导文案。

调用方：`pack-store.js:112`（pack 安装）与 `server/routes/extensions.js:106`（市场卡安装）——两处都中。

### 修法（两处对称补分支）

```js
// credentialConnected():
if (refName === lineCredentials.WIRE_CUSTOMER_CREDENTIAL_REF) {
  return lineCredentials.status(email, "wire-customer").connected;
}
if (refName === lineCredentials.LEX_CREDENTIAL_REF) {
  return lineCredentials.status(email, "lex").connected;
}
// credentialMissingReason() 同位补两分支：
//   "connect your wire line first — no live wire credential"
//   "connect your lex line first — no live lex credential"
```

### 验收

1. 无 registry 凭据、仅 wire-customer 凭据的账号装 `SWPvacoFTftfVngMMFotwQ`（柏讯·产物构建）→ 安装成功、wire-build-mcp 状态 installed；
2. 该账号断开 wire-customer 凭据后再装 → 409 带新文案 + connectPath 指向 `line-wire-customer` 卡；
3. lex pack 同形两测；
4. registry/facet-market/chatwoot/paper-trading 四既有 ref 行为零变化。

**wire 侧配合**：修复上产后，把 wire-tester2 的占位 registry 凭据行断开（`DELETE /api/registry/connection`，source=paste，expires 2026-10-31，是为了完成当日验收加的临时旁路）——**不撤会污染该号的 registry 注入面**。

## 二、P1 · fleet 行更新审计（orchestration 复位者定位的前提）

### 问题

运营号舰队 `fleet_1792786329045_jchvsd` 的 `orchestration` 已两次被复位为 null（10-24 首日 + 当日 13:53 报数实证），复位者未定位；巡线派发边因此消失、连续被拒。

本轮已做**全量代码排查**（结论可直接复用，勿重查）：

- `updateFleet`（`server/routes/fleets-db.js:190`）是外科式更新，orchestration 仅在 `Object.hasOwn(updates,"orchestration")` 时写——无「整行覆盖踩空」类缺陷；
- 三个写 orchestration 的调用点全部有护栏：fleet-architect `draft.fleet?.orchestration != null` 才写（section-less draft 不清除）；PATCH `/api/fleets/:id` 显式带 orchestration 才写；swap 路由只合并 edges；
- fleet-provisioner 对既有舰队只读（dedup 返回现状）；reconcile/cron/mcp 工具面无 orchestration 写点；
- **HTTP 访问日志在复位窗口（08:50–13:53）对 /api/fleets 的 PATCH 为零条**——复位不是走公网 API。

剩余嫌疑：①首日缺陷实录里巡线岗在 fleet MCP 缺位时曾**用 bash 裸读 fleets.db**——同通道的 raw sqlite 写不留任何应用日志（当时平台库是 SQLite，之后 10-22 已切 PG，PG 侧同样无审计）；②未发现的进程内路径。静态分析已到头，需要运行时证据才能定位。

### 需求

fleet 行 UPDATE 审计落痕（复用 wire-provenance-ledger 的台账形态或 paas 现有 audit 简表均可）：

- `updateFleet` 每次调用记：`ts / caller（HTTP 请求身份 or 进程内调用栈首帧）/ fleet_id / 变更字段 / orchestration 旧值→新值`；
- `PATCH /api/fleets/:id` 的访问日志打开（当前 express 不记方法行）；
- （可选加固）orchestration 从非 null 变 null 的写，若 caller 不是该 fleet owner 的 HTTP 会话，打 warn 日志。

### 验收

1. 人为 PATCH 一次 orchestration → 审计可查到 caller 与新旧值；
2. 下次复位复发时，凭审计直接定位复位者（这是本需求的唯一真验收）。

## 三、P1 · 凭据「行从无到有」不触发运行中 cell 交付

### 问题

凭据交付控制器（`dsh-mcp-delivery`，f62f2177）覆盖「行**变化**」的推送，但「该用户**第一行**凭据出现」不触发任何交付——运行中的 cell 进程内缓存的「无凭据」态不会被 `reapplyProfile` 击穿，必须 platform 整体重启才生效。当日两次撞上：

- 13:44–13:55：platform-test 首行重铸后 cell 迟迟不见新行，重启 platform 才恢复（首日报告 P2 缝隙）；
- 15:1x：wire-tester2 首次铸 wire-customer 凭据后，pack 重装持续报 credential-required，日志 `[dsh-profile] omitting ... no live credential for wire-tester2`，同样重启 platform 才过。

### 需求

`lineCredentials.store()` 首次为某 (email, line) 落行时（此前无行），视同凭据变更触发既有交付控制器路径（`dshUpdateMcp` / diff-restart），并把「行从无到有」纳入交付审计。

### 验收

1. 运行中的 cell、无凭据用户 → 浏览器铸首行凭据 → **不重启 platform**，该 cell 的 mcp.patch 组合在 5 分钟内带上对应线服务（tool_search 可见）；
2. 交付控制器日志记一条「credential line created」类事件。

## 四、P2 · Logto wire-console 的 consent 页资产 404（定时炸弹）

### 问题

`wire-console`（i0oxdswqxv4x7te99iukv）是一方应用，Logto 侧 consent 由页面加载中间件自动过（koaAutoConsent 挂在 GET /consent）——但该页引用的资产 `assets/index-Dyvh6LUv.js` 在核心端口、admin 端口、公网边缘三处都 404。现在没炸是因为所有存量用户的浏览器都有长期 Logto 会话（session.grantId 覆盖 scope，consent 根本不出现）。**Grant 14 天过期 + 用户新设备/清 cookie 后的新会话**会撞上这个坏页，登录卡死。

本会话已为 platform-test / wire-tester2 两条 Grant 补行（`oidc_model_instances` model_name='Grant'，exp 14 天，等价用户点一次同意）作为应急；**14 天后同问题复发**。

### 需求（二选一）

a. 修资产供给（sign-in-experience 的 /assets 从正确的源出）；或
b. 该应用改走「无 UI consent」：给存量用户批量补 Grant + 新用户走 experience API 侧的自动授权（若 Logto 版本支持）——但治标，资产坏了还是坑。

### 验收

清掉浏览器 Logto 会话后完整走一次 wire 控制台登录，全程无白页/404。

## 五、P2 · 停机窗口错过的 cron 轮次不补跑（静默丢一次交付）

### 问题

平台重启若覆盖某个 cron 触发点，调度器只记一条 `missed N occurrence(s) during downtime` 日志，**不补跑**，且 `nextRun` 直接跳到下一周期——无人值守舰队因此静默少跑一轮，岗位被判定 `missing`（超出容忍窗）后要靠人发现。

实测（2026-10-26）：运营号舰队巡线 `0 9 * * *`（Asia/Shanghai）= 01:00Z；platform pod 于 **01:33Z** 重启（前一 pod 已停），09:00 那轮落在停机窗内 → 日志仅 `[cron] Task job_1792786329246_f6j77p missed 1 occurrence(s) during downtime`，`lastRun` 停在 10-25T01:17Z、`nextRun` 跳到 10-27T01:00Z，巡线岗 `status=missing`（reason: no execution for over the tolerance window）。

### 需求（二选一，前者更符合无人值守语义）

a. 启动时对「停机期间错过且仍在容忍窗内」的轮次补跑一次（catch-up），并报数标注这是补跑；
b. 至少把 missed 计数暴露到 fleet cockpit / 岗位 evidence，让「少跑了一轮」在控制台可见，而不是只在日志里。

### 验收

人为在触发点前后重启 platform：要么看到补跑回合（带补跑标记），要么 cockpit 明确显示 missed 计数与原因。

## 六、P1 · 回合启动期 `runtime_not_ready` / `runtime restarted mid-turn` 跨舰队普遍失败

> **本文已升级为独立文档** `paas-fleet-turn-reliability-requirements.md`（2026-10-26 全量普查 178 条回合，含 P0 状态不可见/重试语义、P1 timeout 可归因/上游熔断）。本节保留作索引，**排期与验收以独立文档为准**。

### 问题

岗位回合未能启动（或启动后被重启打断）时，平台把失败归到岗位的 `lastTurn.errorKind`，**但状态机仍按「最近执行在容忍窗内」判 `on_duty`**（本文件 §五的 `missing` 是另一条路径，看的是「有没有报数」）。结果是：回合没产出任何结论、没有报数、没有产物，控制台却显示「正常」——无人值守线会把这类静默少跑当成正常。

与 §五 的区别：§五是「触发点被停机覆盖、调度器不补跑」；本节是「**触发了、但回合没跑起来**」，两者叠加时更容易被误读成一次正常但无信号的巡视。

### 实测（2026-10-26 单次横向扫描，`GET /api/fleets/<id>` 逐支读取）

7 支活跃舰队中 **6 支**在最近一轮出现失败回合，合计 11 个失败岗位回合：

| 舰队 | 岗位 | errorKind | 备注 |
|---|---|---|---|
| 柏讯内容舰队 | 巡线岗 | `runtime_not_ready` | 10-25T01:17Z，同一轮触发 §五 的 missing |
| 柏讯情报舆情舰队 | 舆情哨 | `runtime_not_ready` | 10-24T23:40Z |
| 柏讯情报舆情舰队 | 投稿岗 | `runtime_not_ready` | 10-25T00:14Z |
| 柏讯情报舆情舰队 | 情报主编 | `other`（runtime restarted mid-turn） | 10-25T00:24Z |
| 识律政心监测舰队 | 政心主编 | `runtime_not_ready` | 10-25T04:00Z |
| 识律政心监测舰队 | 指标巡检员 | `model_upstream`（502） | 10-25T02:30Z，与 runtime 无关，另一类 |
| 识律评测哨 | 评测哨 | `other`（runtime restarted mid-turn） | 10-25T08:50Z |
| lex 营销内容舰队 | 选题岗 | `timeout`（turn timed out） | 2026-10-26T02:24Z |
| lex 营销内容舰队 | 发布草稿岗 | `timeout` | 10-25T06:20Z |
| 营销·官网内容舰队 | 选题岗 | `timeout` | 10-25T18:00Z |
| 营销·官网内容舰队 | 审读岗 | `timeout` | 2026-10-26T02:14Z |

同批健康的对照：情报舆情「情报巡检员」、政心「投稿岗」、lex「撰稿岗」、官网「撰稿岗/发布草稿岗/评测哨岗」均正常。**说明不是单一舰队配置问题，是平台运行时的启动期/重启期行为。**

### 需求（两条，互不替代）

a. **回合未启动不计入「正常」**：`runtime_not_ready` 应作为「本轮未执行」计入 evidence（类似 missed），并在 fleet cockpit 与岗位卡片显式可见；不要把「有 lastRun 但回合失败」渲染成与成功同形的「正常」。
b. **`timeout` 需要可归因**：`turn timed out` 目前不带耗时/步数/最后一步工具，运营方无法判断是模型慢、工具挂死还是预算设置不当——至少回传已用时长与最后调用的工具名。

### 验收

扫描任一舰队：出现过 `runtime_not_ready` / `timeout` 的岗位，其 cockpit 状态与 `on_duty` 有别，且能一眼看出「本轮没跑成」与「跑了但无信号」的区分。

### 与本次交付的关系

本节由 `wire-content-fleet` 归档前的横向核验发现，**不是该 change 的交付缺陷**（其自身产物/报数/审计判据均已实证通过，见归档 tasks 1.6）。登记在此供 paas 侧排期。

## 七、wire 侧状态（无需 paas 动作，备案）

- **引擎通道开户口径已落**：`AI_CELL_ENDPOINT_MAP`（fd-wire GitOps bcf57e3，三条在位：wire-tester2/wiretester2、drillcust001、pgauto001）。**新付费客户开户步骤**=cell-manager provision+grant（短 slug，PG 库名规则 ≤30 小写）→ relay（socat 尾网端口）→ PATCH registry 条目 proxy_pass_url 触发边缘重渲染 → fd-wire-api.yaml 加一行映射。全程配方在 `openspec/changes/wire-content-fleet/reports/2026-10-24-handoff-execution.md` §三。
- 7 天凭据续期观测照旧（首日数据点：aloadtree 两行正常、tester2 新行自动续 ✓、运营号旧行 invalid_grant 已重铸）。
