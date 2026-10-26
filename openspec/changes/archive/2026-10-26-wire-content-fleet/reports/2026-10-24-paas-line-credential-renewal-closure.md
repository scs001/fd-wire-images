# 回报：缺陷⑤（凭据无人值守续期）paas 侧闭环（2026-10-24 晚）

> 对应：`reports/2026-10-24-fleet-live-run.md` 缺陷⑤ + `docs/paas-line-credential-renewal-requirements.md`（需求文档全文）
> 结论先行：**双缺陷已设计、实施、出片、上产**（paas `2b8cc9a9` → 镜像 digest `sha256:e1e6daa1`，生产双 pod 已在跑，迁移 102 生产实证）。wire（柏讯）侧**零改动**确认成立——需求文档的判断兑现：authorize URL 平台自拼、追加 scope 不需要 wire 动作，wire 仍只验 bearer、计量语义不变。

## 一、spike 实证（需求评审前置的三问，2026-10-24 真机跑完）

用真实会话手拼 authorize URL（带 offline_access）+ 服务端自兑 code + 连刷两次：

1. **refresh_token 正常发放**：wire 的 Logto client（`i0oxdswqxv4x7te99iukv`）对 offline_access 直接发 refresh_token——auth config 里本就宣告了 `offline_access`，**paas 侧 `line-credentials.js:174` 的 scope 过滤是唯一拦路者**，需求文档的根因定位逐字命中。
2. **rotation 开启且宽松**：每次刷新返回新 refresh_token；**旧值在宽限窗口内重放仍 HTTP 200**——并发续期竞态天然可存活（paas 侧仍做了单飞锁，不赌宽限窗）。
3. **refresh_token 为不透明令牌**（非 JWT、无 exp 可读）——租约长度客户端不可自读。Logto rotation 语义=每次刷新重锚定租约 → **持续刷新即续命**；唯一断链条件是「平台停机超过租户 refresh TTL」，断链时行标 stale、用户重连一次重建（诚实降级，即验收③语义）。
4. 附带：首连 offline_access 在 `prompt=none` 下返回 `consent_required`，**interactive 通道免屏自动授权**（first-party client 无 consent UI）——需求文档五节预判的冲突被 paas 既有 interactive 重试机制完整吸收，零额外工作。

## 二、交付对照（需求 P1/P2 → 落点）

| 需求 | 落点 | 状态 |
|---|---|---|
| P1 铸币拿 refresh token | 铸币流放行 `offline_access`，token 响应的 refresh_token 随凭据行存储（迁移 102 新列；**投影 SELECT 显式列名不含新列**——read 面逐字节不变） | ✅ 已上产 |
| P1 双触发续期 | ① 每 cron 回合开始前（`runCronJobTurn` 收口，`exp-now<30min` 先续再跑）；② 后台 10min 巡检扫将到期行（`setInterval.unref`） | ✅ 已上产 |
| P1 rotation 处理 | 新 refresh_token 与新 ID token **同一次行更新原子落库**；并发触发 `Map<email\|line>` 单飞（一次请求） | ✅ 单测钉住 |
| P1 失效兜底 | `invalid_grant` → 行标 stale（**不清行**）、连接卡「需重连」；回合拿明确未连接态 | ✅ 单测钉住 |
| 伴生 401 语义 | ID-token 线 `markInvalid` 从整行清除改为「有链标 stale / 无链保持清除旧 UX」；401 钩子（`dsh-events.js`，**需求文档"拿不到 401 事件"的前提经核不成立——钩子已在生产运行**）改 refresh-first | ✅ 已上产 |
| P2 热交换必修 | `dshUpdateMcp` 写 patch 前后 **diff Authorization 头值**：变化且空闲→restart（重 spawn 读盘，生产实证路径）；忙→250ms poller 回合边界排空，**在跑回合零打断**；纯 server 增删维持 HMR 不变。无人值守链路零 HMR 信任 | ✅ 已上产 |
| P3 内部长效令牌 | spike 全绿，**永久搁置** | 不需要 |

计量归属与审计不变：仍是每用户一行，注入的还是该用户的 token。工具岗（T1）本就服务端现读 DB，零改动受益。

## 三、上产证据链（2026-10-24 晚）

```
paas 2b8cc9a9（17 文件，测试 25+e2e 537/0+validate 绿）
→ 公开快照线 scs001/platform-image-verify f8f1505（GHA image run 40065765778 绿 3m59s，smoke 门含）
→ relay OK yizuo/platform:sha-f8f1505（13:26:34）
→ skopeo ccr/hkccr digest 双等 sha256:e1e6daa1…
→ GitOps fd-infra-deploy d6767a1 双清单（platform/platform-demo）滚 sha-f8f1505
→ ArgoCD Synced Healthy，双 rollout 完成，pod imageID==digest 三核过
→ pod 日志 [db] applied migration v102（生产库已带 refresh_token 列）
```

## 四、验收状态对照（需求三、验收四条）

1. **7 天 09:00 巡线读面全 200、零浏览器** → 机制已上产，**观测中**（观测现场仍 `fleet_1792786329045_jchvsd`）。✅ **前置动作已完成（2026-10-24 05:43Z）**：admin 两线重连（interactive 免屏自动授权），生产行 `hasRefresh=1`（43 字符不透明 rotator）、stale=0、exp 全新；pod 日志实证过期行从 patch 省略（5 servers）→ 两线回归（7 servers）——缺陷⑤「未接入」的机理在生产反转。**7 天观测自 2026-10-24 起算**。
2. **首连一次后 30 天不再点连接** → 机制成立（rotation 每次重锚定租约）；租约绝对长度不可自读，若租户配置为短绝对租约且恰逢长停机，行为=stale+重连（符合验收③语义，不假成功）。30 天观测随 7 天观测一并滚动。
3. **refresh 失效显 stale、报「未接入」** → 已实现+单测三分支（续期成功/续期失败标 stale/无链旧行保持清除）。
4. **token 与 refresh token 不出现在任何 read 面/日志** → 投影 SELECT 不含新列+投影断言单测；续期日志只记 email/线/成功失败。

## 五、给 wire 侧的两个知会（无动作要求）

1. **paas 侧 `test-wire-tool-risk` 对账测试当前红**：wire 侧 checkout 新增的 `wire_artifact_dashboard_create` / `wire_artifact_doc_create` 未吸收进 paas 的 vendored 快照。paas 台账已记为已知存量（与本 change 无涉），修复=paas 侧跑 `scripts/refresh-wire-tool-surface.mjs`——wire 侧下次同步工具面时知会一声即可。
2. 观测期内若 wire 侧调整 Logto 租户 refresh TTL 或 rotation 策略，请同步告知 paas（会影响验收②的口径）。

——paas（壹座）侧 2026-10-24，change `add-line-credential-renewal`（openspec 四工件+台账 §15.25 全记录）

## 六、复核回应（P2 缝隙 + admin 链失效，2026-10-24 14:5x）

**① P2 缝隙根因已定位并修复上产（paas `f62f2177` → 镜像 `sha-0bcf53e`，双面已滚）**：fleet cell 的时序复盘结论——**13:46 的组合（写文件）是对的，卡点在交付**：`bridge.restart()` 在另一个 restart 进行中时直接 throw（dsh-bridge 的并发拒绝），而 reapplyProfile 的 catch 把它吞掉——交付静默丢失且无重试；pack 重装只走 skills patch 不触发 mcp 重交付，子进程于是滞留旧 patch 直到 13:55 pod 重启。你们看到的「持续 no live credential（分组 warn）」很可能是 connector/lex 的合法 omit 行（platform-test 本就无这两线凭据）与交付失效叠加的表象。加固包：restart 碰撞重试（2s/4s/8s 三档）+ 流式回合 defer 到回合边界排空 + **5 分钟硬上限强制交付**（wedged 流不得扣留凭据）+ 全分支日志（`[mcp-delivery]` 前缀，每分支可诊断）+ 回合前排空。控制器 7 分支单测。**本镜像上 fleet 行 14:50 窗口的续期交付日志将作为加固的实证点。**

**② admin 两行首轮续期 `invalid_grant` 的判定材料（需 wire 侧查一处日志）**：admin（aloadtree）两链 13:43 铸、14:13 首刷双双 invalid_grant → 按设计翻 stale；同批铸币的 fleet 链（platform-test，13:46 铸）14:16 首刷**成功**。paas 侧已重铸 admin 两链（14:24）并做即时探针：**新链 refresh 200 + rotation 写回**——铸币产物健康，排除 mint 出死链。指向：**aloadtree 的 grant/session 在 13:43-14:13 之间被外部吊销**（与多浏览器会话登录时序强相关——13:42/13:43 前后各有一次 admin 登录铸币）。请 wire 侧查 Logto 租户审计日志该时段 aloadtree 的 grant/session 事件定案；若租户有「新登录吊销旧 grant」类策略，paas 侧把「重连后 30 分钟内不重复登录铸币」写进验收操作纪律即可。admin 两链已恢复（exp 15:24），续期窗口 14:54 起。

**③ 观测注记采纳**：tool_search 假阴性的判读纪律（先查 transcript 有无找到过工具再下平台结论）已并入 7 天观测的操作口径。

## 七、链死亡机理定案（修正版，2026-10-24 深夜终稿；替代本节早前版本的 TTL 推断）

夜观测继续跑出了一个更干净也更反直觉的定案，**早前版本的「TTL 31-33min」「grant 绝对租约」两个模型均被后续数据证伪**（aloadtree 链在无人干预下自持 8.5h+ 多轮 rotation；「裸登录杀链」被实验推翻——23:21 裸登录后四链续期全部正常，23:22 那条 lex invalid_grant 是瞬态，15 分钟后同 rotator 自愈）。

**唯一有实证的杀链机理：同 user+client 的新授权（再点「连接」/再铸币）作废旧链。** 两次精确对齐：①壹座 22:1x playwright 重铸 admin 两行，杀掉了当时已自持 **8.5 小时**、多轮 rotation 的旧链；②贵侧 23:0x-16 重连 fleet 行，杀掉了 fleet 旧链。其余历史死亡（含 10-24 凌晨首日、当日 14:1x 的 admin 首刷失败）时点全部落在人工授权活动窗口内，精确事件类型待贵侧审计日志定案（候选：Logto 单活跃 grant 语义=默认行为；也不排除叠加其他策略）。

**当前状态（23:59）**：四条链（aloadtree wire+lex、platform-test wire、wire-tester2 wire）在固定节奏（rotation 后 15min 续/5min sweep）下**连续自持 >1h、全部成功**，扛过一次裸登录；fleet 链贵侧重连后已两轮续期成功。

**给贵侧的三件事**（全文见壹座仓 `docs/wire-logto-grant-policy-requirements.md`）：
1. **运营纪律（立即）**：观测期内（至 10-31）勿再对 aloadtree / platform-test 手工点「连接」或重铸——每次授权作废旧链、重置 7 天观测计时；只读操作不受限；确需重连请知会壹座重置起算点。
2. **机理查证（建议 ~10min）**：Logto 审计日志筛全天两用户的 grant/session 事件，确认吊销事件类型（单活跃 grant 语义 vs 额外策略）；若另有 TTL 类配置一并调整。
3. **结构性方案（5.x 前决策）**：舰队专属服务账号（无人工交互登录），壹座现网机制零改动即长期无人值守。

判读注记（并入观测口径）：巡线 transcript 先查有没有找到过工具再下平台结论（tool_search 假阴性）。

## 八、wire 侧查证结论（2026-10-25 凌晨回填，对应 §七「三件事」与交接单 §四）

> 取证方式（全程只读，未触发任何登录/授权）：Logto 1.42.0（xinru-master docker `fd-logto`，DB `logto`）审计表 `logs`、`oidc_model_instances`、容器内部署版源码（`packages/core/src` + logto fork 的 node-oidc-provider）。

### 8.1 机理定案：不是 revoke，是 SPA refresh token 的会话绑定失效

**2.2 要筛的「吊销事件」在审计日志里不存在**：10-24 全天两 client 零 `RevokeToken`；登出/会话销毁/换 grant 均不产审计记录。13:42/13:46 两次杀链（14:16:55、14:47:57 两批 `invalid_grant`）的报文全部是 `error_detail: "refresh token not found"`——而 DB 里 grant 行、RT 行当时都还活着。真凶在 fork 的 `oidc-provider/lib/models/mixins/is_session_bound.js`：SPA 公共客户端的 RT 带 `expiresWithSession:true + sessionUid`，**每次刷新三重校验**，任一不满足即报 not found：

1. 铸币时的浏览器会话（sessionUid）仍存活——**会话销毁即杀链**；
2. 会话 accountId 未变——同会话换账号即杀链；
3. 会话当前 `grantIdFor(clientId)` 仍等于 token 的 grantId——**铸币会话上再授权（换新 grant）即杀链**。

由此修正 §七的定案：**「同 user+client 新授权作废旧链」只在授权发生在铸币会话上时成立**。反例实锤：23:57:36 另一轮对 platform-test 的重铸（见 8.3）之后，22:58 铸的 fleet 链 00:17 仍在成功 rotation——独立会话的新授权不杀链。裸登录不杀链（23:21 实验 ✓，与代码语义一致）。13:43 链死于 playwright 临时会话销毁、13:46 链死于 ZCode 会话销毁、8.5h 链死于 22:40 同会话（headless）再授权换 grant——全部落入上述三条件，无第四种机理。

### 8.2 更正一处前提：SPA RT 有 14 天绝对租约，rotation 不续命

部署版 `packages/core/src/oidc/defaults.ts` 的 `refreshTokenTtl` 对 `web + clientAuthMethod:none`（即 SPA）返回 `RotatedRefreshToken.remainingTTL`——**rotation 继承剩余寿命，铸币起 14 天封顶**（代码注释原文："do not have infinite expiration through rotation"）。数据互证：14:24 链 16 轮 rotation 后 RT `exp` 仍钉在 `iiat+1209600s`。Session TTL 同为 14 天。`logto_configs` 为空=全默认，**无租户级 TTL 旋钮**（调整=换镜像打补丁，不建议）。

后果：§四验收②「30 天不再点连接」口径**不成立**——链在铸币后第 14 天必死。现网四链（grant iat 22:25/22:40×2/22:58）将于 **11-07 22:25–22:58 集体到点**。7 天观测（至 10-31）不受影响。壹座侧建议把验收②改为「≤13 天周期自动重铸」。

### 8.3 对三件事的答复

- **2.1 运营纪律（修正，实际更宽松）**：人工在自己浏览器登录/退出这两账号**不受限**（独立会话不碰链，双重实证）。真正危险的是：①重跑任何**复用旧会话 cookie** 的铸币/登录脚本（同会话换 grant=杀链，条件③）；②在铸币会话环境里登出或换账号。只读巡线照旧。
- **2.2 查证结论**：见 8.1/8.2，非租户配置问题，无需调整。
- **2.3 结构性方案（修正）**：服务账号只解决人机共享互踩，**不解决 14 天墙**。5.x 正解=**每 cell 一个服务账号 + <14 天周期自动重铸**（壹座 headless 铸币配方已具备）。备选：舰队场景建机密客户端（Traditional Web 应用，rotation 可续命），但 session 绑定语义未验证、应用类型迁移动静大，不作首选。

### 8.4 附带发现

1. **23:21:54 的「瞬态 invalid_grant」实为 `client mismatch`**：壹座 rotator 把 lex 的 RT 刷到了 wire 端点（张冠李戴），链无恙——建议壹座修 rotator 的账号×线配对。
2. **交接单未记录的第五/六次铸币（python-requests，10-24 14:44–14:54、22:17–22:25、23:57 三批）**：来源=xinru 宿主机本地直打 Logto 3001（审计 `ip=::ffff:172.18.0.1`，绕过网关无 XFF），当天宿主机无 SSH/sudo/cron/tmux 任何执行痕迹，事后无法唯一指认执行通道（候选=当时存活、现已退出的 hostNetwork pod exec 或宿主代理向量）。**定性：非未授权方**——铸的就是 wire-tester2/platform-test 两条观测链本身，用正规测试账号口令，属舰队自家验证/铸币工具链。运营含义：宿主本地铸币在审计面完全不可归因，**后续所有铸币统一走 platform mint 回调通道**（`/api/lines/*/mint/callback`，审计可见 UA/来源），顺带消除同会话重铸杀链风险。
3. 22:58「贵侧重连 fleet 行」实为 `mint-line/1` UA（Mac 发起）——交接单把壹座铸币件记成了贵侧动作，不影响结论。
