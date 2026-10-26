# 萬星/壹座侧需求：产品线凭据无人值守续期（line credential renewal for unattended fleets）

> 来源：fd-wire change `wire-content-fleet` 任务 1.6 实跑首日（2026-10-24，`reports/2026-10-24-fleet-live-run.md` 缺陷⑤）
> 对象：paas（壹座）仓。wire（柏讯）侧在推荐方案下**零改动**。
> 状态：**已闭环（2026-10-24 晚）**——paas 侧当日立项（openspec `add-line-credential-renewal`）→ 实施 → 出片上产（镜像 digest `sha256:e1e6daa1`，迁移 102 生产实证）。spike 三问全绿（refresh_token 发放/rotation 宽松/持续刷新即续命），P3 搁置。回报全文：`openspec/changes/wire-content-fleet/reports/2026-10-24-paas-line-credential-renewal-closure.md`。剩余=7 天验收观测（前置：旧凭据行重连一次）。

## 一、问题（一段话 + 实测证据）

壹座的产品线操作凭据（wire-customer / lex）是**短命 Logto ID token**（实测 TTL 恰 3600 秒），存每用户一行，铸币走浏览器 PKCE（`prompt=none` 静默），**续期依赖用户浏览器的 Logto 会话**。舰队岗位是无人值守 cron 回合，没有浏览器——token 过期后巡线只能报「未接入」。实测：凭据 00:12Z 铸入，01:13Z 过期，随后巡线三轮全部 `[unauthorized] ExpiredSignatureError (HTTP 401)`，直到 platform 整体重启才恢复读面。日节奏（每天 09:00）的巡线**必然**每天撞一次过期；外部客户 cell 同理。连接卡片上的「静默铸造，自动续期」在无人值守场景不成立。

**根因在代码**：`server/routes/line-credentials.js:174` 把 `offline_access` 从 scope 里显式剔除——平台从不向 Logto 要 refresh token，服务端续期能力在铸币那一刻就被放弃了。

伴生缺陷（同一张工单必须一起收）：**凭据热交换不进运行中的 dsh 子进程**。重铸后 `mcp.patch.yml` 已重写（新 token 在文件里），但 dsh 子进程连读三轮旧 token（401），直到 platform 重启重 spawn 才生效。即使 P1 做了服务端续期，不同时修这个，续期结果到不了回合里。

## 二、已核实：现状链路（免重查）

| 环节 | 位置 | 事实 |
|---|---|---|
| 铸币起点 | `server/routes/line-credentials.js:120-196`（mint/start） | 浏览器 PKCE；非交互走 `prompt=none`；**scope 过滤掉 `offline_access`**（:174） |
| 回调落库 | 同文件 mint/callback | code→ID token 服务端兑换，`lineCredentials.store()` 每用户一行，只写不读 |
| 注入 | `dsh-profile.js` 写 `mcp.patch.yml`，headers.Authorization 直嵌 token | 每次凭据变更 `reapplyProfile → dshUpdateMcp` |
| 热交换 | `server.js:721-760` | patch 文件原子重写后靠 cordis watcher HMR；**实测未生效**（子进程拿旧 token 直到重启） |
| token 形态 | 实测 JWT | `iat=…0785 / exp=…4385`，TTL=3600s，issuer=auth.finddatatech.cloud，client=各线 SPA |

## 三、需求

### P1 · 服务端续期：拿 refresh token，摆脱浏览器依赖（推荐，治本）

铸币流改拿 `offline_access`（首连是交互式的，本来就要用户点「连接」，顺手 consent；后续静默），token 响应里的 `refresh_token` 随凭据行存储（只写不读口径不变）。续期在**服务端**完成，不再需要浏览器：

- **触发**（两道，都做，便宜）：① 每 cron 回合开始前，岗位凭据若 `exp - now < 30min` 则先续再跑；② 后台低频巡检（如每 10 分钟）对将到期行续期。401 触发式续期不做（MCP 客户端层拿不到 401 事件，成本高收益低）。
- **rotation 必须处理**：Logto refresh token 是轮换的——每次刷新返回新 refresh_token，必须落库新值，丢了链就断。
- **失效兜底**：refresh 被拒（用户改密/吊销）→ 行标记 stale，现有「调用键 401 时标记为需重连」UI 语义复用；回合侧拿到的是明确的未连接状态，不是静默 401。
- 计量归属与审计不变：仍是每用户行，注入的还是该用户的 token。

**验收**：
1. 运营号 cell 舰队巡线岗（cron 0 9 * * *）**连续 7 天 09:00 回合读面全 200，全程零浏览器操作**；
2. 用户首连一次后 30 天内不再点连接，读面持续可用；
3. refresh 失效场景：行显 stale、连接卡提示重连，巡线报「未接入」而非假成功；
4. token 与 refresh token 不出现在任何 read 面/日志（现状口径保持）。

### P2 · 凭据变更必须到达运行时（伴生必修）

P1 续出来的新 token 要在**下一回合**就生效，不允许依赖 platform 重启：

- 首选：修 HMR——查 cordis watcher 为何未触发/未重连（子进程带 `--patch` 启动、文件已原子重写，实测仍拿旧 token）；
- 备选：`dshUpdateMcp` 检测到 Authorization 头**值变化**时改走 `dshBridge.restart({ mcpPatchPath })`（代码路径现成，server.js:752），串行锁保证不与在跑回合对撞；空闲时执行，忙则推迟到回合边界。

**验收**：重铸/续期后 ≤5 分钟内起的下一个回合用新 token（以巡线回合读面 200 为证），platform 全程不重启。

### P3（可选止损，非推荐）· 内部狗粮专用长效服务令牌

若 Logto SPA client 对 offline_access/rotation 有政策或实现障碍：给内部运营号单独发一把 wire 侧长效服务 token（走 wire 的内部令牌通道），只注入舰队岗位回合。**缺点**：外部客户 cell 用不了（客户凭据轨要求本人 token 计量归属），问题只被推迟到 5.x 外部开放；且需要 wire 侧配合发牌。故仅作 P1 受阻时的止损项。

## 四、非目标

- 不改 wire（柏讯）侧鉴权与计量语义（P1 下 wire 仍只验 bearer）；
- 不做跨用户凭据共享、不做平台级超级 token；
- 不把 ID token TTL 拉长（ weakening 交互场景安全面，否决）；
- 舰队编排面（派发边、报数）不动——那是 wire-content-fleet 已交付部分。

## 五、边界与安全

- refresh token 存储沿用凭据行（DB 内，与现有 secrets 同级）；如需加密，按 paas 现有 secret 惯例，不引入新 KMS 依赖；
- 续期日志只记「哪个 email 的哪条线在何时续期成功/失败」，不落 token 本体；
- `prompt=none` + offline_access 首连冲突：若 Logto 对未 consent 过的 offline_access 拒绝静默铸币，把首连改为 `interactive=1`（用户本来就在点连接），续期维持静默。

## 六、paas 侧对照文件

```
server/routes/line-credentials.js      # 铸币流（scope 过滤在 :174）+ 回调兑换
line-credentials.js                    # 凭据行存取（liveToken/markInvalid）
server.js:721-760                      # dshUpdateMcp 热交换 vs restart 分叉
dsh-profile.js                         # mcp.patch.yml 写入（headers.Authorization）
server/cron-runner.js                  # 回合启动钩子（P1 触发点①的落点）
```

## 七、wire 侧对应（供对照，无动作）

```
fd-wire/openspec/changes/wire-content-fleet/reports/2026-10-24-fleet-live-run.md   # 缺陷⑤证据
fleet_1792786329045_jchvsd（platform-test cell）                                    # 7 天验收的观测现场
```
