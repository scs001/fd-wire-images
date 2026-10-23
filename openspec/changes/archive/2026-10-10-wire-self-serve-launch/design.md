# Design — wire-self-serve-launch

## Context

侦察实证的关键现状（file:line 见 proposal 与侦察记录）：

- **档位已 DB 驱动**：`billing_plans`（月费微元/含额行数/超额单价/is_active）seed 插入制，`included_lines` 一列已同时驱动结算、物化额度硬停、预警。新增/改档零代码，缺的只是新四档 seed 与公开端点。
- **消费链半通**：结算/对账/预警/sub2api 客户端（含 `ensure_customer()` 代建账户）全部就绪，但 admin 开户端点只落本地 `billing_customers` 行、不代建 sub2api 账户，`sub2api_user_id` 靠手工粘贴。
- **当月拦截在 registry 预检**：wire api 自身无逐请求预检；预检现查 sub2api 余额（402）+ caller 映射（30–60s TTL、fail-closed）。免费档 sub2api 余额恒零，直接复用余额门会全量误杀。
- **登录样板可平移**：Logto（`auth.finddatatech.cloud/oidc`）discovery 200、PKCE S256、claims 含 organizations/roles；lex 线有整套 SPA+PKCE+sessionStorage+ID token 本地校验（`fd-lex/web/src/console/auth.ts`、`fd_lex_api/console_api/auth.py`）。**M2M /api/v2 仍 404**——Logto 内建应用/挂角色只能 Admin Console UI 手工。wire 在 Logto 仅有 org `wire-customers`（tdwwx4not5ck），无 SPA 应用。
- **授权面不依赖 Logto 组**：开户自动化的 wgk- key 组快照是合成组（`["wire-customers","wire-cust-<cid>"]`，不经 Logto）；registry 侧另有角色 `wire-customers`+scope `mcp-business-wire-execute` 的存量口径。
- **前端占位件**：`console/customer.ts`（localStorage X-Customer-Id，默认 demo-customer）、`admin/api.ts`（DEFAULT_ADMIN_TOKEN="dev-admin-token"，bundle 公开值）、`views/LoginView.tsx` 死按钮；后端四处客户路由头注释「接 wire-customer-identity 后替换」。billing 侧已留 Logto username→Customer resolver 桩。

## Goals / Non-Goals

**Goals:**
- 四档定价全链生效：seed→公开端点→定价页活取→行数门→结算，一处数据多处一致。
- 自助注册到首次 200 调用全自动，无人工环节、无 Logto M2M 依赖。
- 客户侧身份切换为 Logto sub 映射，占位登录退位；admin 双闸。
- 用量对客户透明，与行数门/结算同源。

**Non-Goals:**
- 在线自助充值/支付页（升档首期=联系开通+内部操作；lex 线的入金端点配方可在后续 change 平移）。
- Logto M2M 自动化修复、org 自动挂接；IdP 单点治理（挂账，接客前硬前提另线跟踪）。
- registry 侧存量角色口径（`wire-customers` 角色）的平迁——登录门禁不依赖它，不动。
- 需求单页点亮（现状已是独立视图，本轮不动其行为）。
- 主官网 fd-official-web 文案（伴生 change `wire-tab-refresh`）。

## Decisions

### D1 免费档不被余额门误杀：行数门为主门，余额门条件化

预检顺序：caller 映射 → **行数门**（新增）→ 余额门（仅 `overage_unit_price>0` 的档位）。行数门数据来自 wire api 新端点 `GET /internal/quota/{caller}`（当月 metering_records 聚合 rows_returned vs 档位 included_lines），registry 预检以 caller-map 同款模式接入（分钟级 TTL、探针闸门、fail-closed）。备选「免费档给 sub2api 预存余额」被否——引入伪余额账，对账口径被污染；「当月不拦月底结算」被否——违背用完即停裁决。免费档照常代建 sub2api 账户（计量归集与 caller-map 需要 sub2api_user_id 作为 caller 身份键），只是不再靠余额拦人。

### D2 身份模型：Logto 只做身份层，映射自管，绕开 M2M 404

照抄 lex 样板但**关掉 org 门禁参数**：wire api 校验 ID token 签名与标准 claims，不要求 organizations claim 命中。映射表 `billing_customers` 增 `logto_sub` 唯一列（新迁移），登录身份→sub→Customer 行。理由：自助注册要求用户免人工获得授权，而 Logto 组/org 自动化（M2M）不可用；授权面走 wgk- key 组快照本就不经 Logto 组，org claim 门禁对本系统无增量安全收益，只增加一道人工挂组环节。备选「照抄 org 门禁+人工审批」被否——免费档获客价值归零。

### D3 首登开户：wire api 为触发方，cell-manager 为执行方

wire api 新增 console 端点：登录校验后查 sub 映射，无映射则以服务身份调用 cell-manager 既有开户 ceremony（免费档 plan=free、跳过 cell 配给）。幂等由 ceremony 既有语义承担（重复开户、重复铸 key 幂等报错）；失败中止+可重试，不留半开户态（映射行以 ceremony 成功为前提落库/置有效）。**不改 cell-manager ceremony 语义，只加触发入口与免费档跳过 cell 参数。** 备选「前端直调 cell-manager」被否——凭据面外露。

### D4 定价呈现：billing_plans 为唯一事实源，前台活取

新增公开只读 `GET /api/v1/plans`（仅 is_active 公开档），定价页/首页/EN 全活取；i18n 档位键删除。自定义档=admin 建档（`POST /internal/admin/plans`，`is_public=false`）+指派；旧三档 seed 置 `is_active=false`（新迁移，无存量客户零迁移）。EN 价格符号 `¥`（与 ZH 一致，避免双货币口径）。副标题「超额阶梯单价」表述替换为「用完即停、次月恢复」。

### D5 admin 双闸：角色门禁在前端导航与后端 API 各自成立

后端：`/internal/admin/*` 在 X-Admin-Token 闸前叠加 Logto Bearer 校验 + `wire-admin` 角色 claim（无角色 403）；X-Admin-Token 保留为第二道闸（纵深，服务间调用仍走令牌）。前端：登录用户无 `wire-admin` 时不渲染后台导航，token 输入框退场（服务调用自动附带登录令牌+服务令牌）。`wire-admin` 角色与初始管理员挂接走 Admin Console UI 一次性手工（M2M 不可用，量小可接受）。

### D6 用量透明：复用既有聚合，只加客户侧视图

内部已有 `GET /internal/admin/customers/{id}/usage`（caller=sub2api_user_id 联查聚合）。新增 `GET /api/console/usage`：以登录映射的 Customer 为锚做同款聚合（当月 rows_returned、档位含额、余量），与行数门、结算同读 metering_records。前端 ConsoleLayout 用量页接该端点，占位退位。

## Risks / Trade-offs

- [registry 行数门探针跨仓依赖] → 探针失败 fail-closed 拒绝并标注原因（与 PREFLIGHT_UNMAPPED=deny 同哲学）；上线顺序先 wire 端点后 registry 探针，灰度期行数门未生效前额度超限最多产生当月超额用量（无超额计费、结算按档含额），财务风险上限=一个月的免费/低档读取量。
- [首登开户风暴/滥用] → 邮箱验证前置；免费档无 cell（容器成本=0）；每身份一户；suspend/key 吊销/退租幂等治理现成。若仍出现批量注册，后续加邀请码或速率限制（另立项，不阻塞）。
- [T1/T2 静态令牌已进会话日志] → 上线窗口内轮换 `fd-wire-env`（admin/internal/metering 三令牌）并滚动 pod；属运维动作，列入任务。
- [IdP 单点（xinru-master OOM 前科）] → 登录不可用时存量 wgk- key 调用不受影响（key 校验不经 Logto）；受影响面=新注册与控制台操作。挂账接客前硬前提。
- [EN/ZH 双语文案与四卡布局改动面] → PricingView 测试同步重写；i18n 键删改跑既有 i18nKeys 测试门。

## Migration Plan

1. 迁移与 seed（alembic 新版本）：billing_customers 加 `logto_sub` 列、四档 seed（free/basic/standard 公开 + 旧三档置禁用）、plans 公开标记列。
2. wire api：console auth 模块 + /api/v1/plans + /api/console/usage + 额度查询端点 + admin 角色闸 + 开户接线（ensure_customer + 首登触发）。
3. registry：预检行数门探针 + 余额门条件化（独立小 PR，依赖 wire 额度端点可用）。
4. web：登录/退位件/定价四卡/用量页/首页文案。
5. Logto UI 手工（一次性）：wire SPA 应用（回调 `/console/callback`、CORS wire 域）+ `wire-admin` 角色 + 挂管理员。
6. 令牌轮换 + Secret 滚动（与 5 同窗口）。
7. 验收：免费注册端到端（注册→首登→得 key→200 调用→到量 1000 行被拒→次月恢复）、¥10/¥50 档语义、admin 双闸、旧三档不可见。
8. 回滚：registry 探针独立开关可先关（退回仅余额门）；wire api/web 按镜像回滚；seed 回滚=旧三档重新置 active（无数据丢失，logto_sub 列可空）。

## Open Questions

- 免费档「我的数据」升级引导 CTA 形态（mailto vs 站内表单）——文案级，apply 期定即可。
