# wire-self-serve-launch

## Why

柏讯门户（wire.finddatatech.cloud）自 2026-10-05 上线后服务面已扩了一批（我的数据/上传物化/产物溯源/开户自动化全部生产实证），但门户本身还停在演示态，承接不了自助获客：定价是高客单三档（¥1,999–19,999）且 i18n 硬编码（副标题宣称「后台可配」名不副实）；控制台与内部后台仍是开发占位登录（手填 X-Customer-Id / X-Admin-Token，客户页 401 红条常态化）；开户端点不代建 sub2api 支付账户（前台标价与 402/结算行为脱节）；控制台「用量」页占位。本 change 把门户从演示态转为自助服务态：低门槛四档定价 + 自助注册登录 + 计费闭环接线。

## What Changes

- **定价四档改版**：免费（¥0，含 1,000 行/月）、基础（¥10，含 100,000 行/月）、标准（¥50，含 1,000,000 行/月）、自定义（询价，admin 建档指派）。全部档位无超额单价——用完即停、次月恢复。旧三档（starter/professional/enterprise）seed 禁用（is_active=false，无存量客户零迁移）。
- **兑现「后台可配」**：新增公开只读档位端点（GET /api/v1/plans，仅 is_active），定价页/首页活取，i18n 硬编码退位；admin 侧新增自定义档建档端点（POST /internal/admin/plans）。
- **sub2api 建户接线**：开户（人工与自助）自动代建 sub2api 支付账户——`ensure_customer()` 已有实现，接入开户流程，消除手工粘贴 sub2api_user_id。
- **当月行数门**：wire api 新增额度查询端点（当月 rows_returned 聚合 vs included_lines）；registry 预检加行数门（到量即拒）；sub2api 余额门降位为仅可超额档（overage_unit_price>0）生效——免费档 0 余额不再被误杀。
- **正式登录（Logto）**：SPA + 授权码 + PKCE（平移 lex 样板），wire api 做 ID token 本地校验；不要求 organizations claim 门禁（绕开 Logto M2M /api/v2 404，授权面走 wgk- key 组快照，本就不依赖 Logto 组）。免费档注册即得 wgk- key（邮箱验证防滥用），不含 cell；cell 自 ¥10 档起配。
- **首登自动开户**：登录后按 Logto sub 映射 Customer；无映射且首次 → 自动触发既有 cell-manager 开户全套（Customer 行 + wgk- key + sub2api 账户 + caller-map + registry 授权束），免费档跳过 cell 配给。
- **admin 双鉴权**：X-Admin-Token 后端闸保留；前端叠加 Logto `wire-admin` 角色门禁（UI 手建角色，量小无自动化需求），占位 token 输入框退场。
- **控制台用量页点亮**：客户自助查看当月用量（rows_returned）/额度余量/近期调用，数据源=既有 metering_records。
- **站内官网文案更新**：首页价值主张覆盖已上线服务（我的数据/上传物化/产物），删除「控制台上线中」过时口径；EN/ZH 双语同步，EN 价格符号用 ¥。
- **主官网 wire tab 更新**：在 fd-official-web 仓以伴生 change 处理（文案+「查看定价」跳转，不在主官网标价）。
- **令牌卫生（运维）**：曾进入会话日志的两枚静态内部令牌（fd_wire_admin_token 等）轮换 + k8s Secret 滚动。

## Capabilities

### New Capabilities

- `portal-pricing`: 四档定价模型（免费/基础/标准/自定义）、公开档位端点、定价页活取、用完即停口径、自定义档建档与指派。
- `console-auth`: Logto 正式登录（PKCE + ID token 本地校验）、Logto sub ↔ Customer 映射、开发占位登录退位、admin 角色门禁。
- `self-serve-onboarding`: 首登自动触发开户 ceremony、免费档边界（有 key 无 cell）、滥用治理（邮箱验证 + suspend/吊销）。
- `usage-transparency`: 客户侧用量查询端点与控制台用量页（当月用量/额度余量）。

### Modified Capabilities

- `caller-preflight-map`: 预检新增行数门（当月 rows_returned ≥ included_lines → 拒，不达上游）；sub2api 余额门收窄为仅可超额档生效；免费档 0 余额调用不再被余额门误杀。

## Impact

- **fd-wire/api**：billing（plans seed 新四档 + ensure_customer 接线 + 自定义档建档端点）、额度聚合查询端点、console auth 模块（新增，PyJWT/PyJWKClient）、admin 角色门禁、首登开户触发。
- **fd-wire/web**：PricingView 四卡活取、LoginView/customer.ts/admin token UI 三占位件退场、ConsoleLayout 用量页、首页文案（i18n 双语）。
- **registry（跨仓，小改）**：preflight 行数门探针 + 余额门条件化（复用 caller-map 同款模式与 TTL 框架）。
- **cell-manager**：被 wire api 以服务身份触发开户（既有 ceremony 不改语义，只加触发入口）。
- **Logto（UI 手工，M2M 不可用）**：新建 wire SPA 应用（回调 /console/callback、CORS wire 域）+ wire-admin 角色 + 挂现有管理员。
- **fd-official-web（伴生 change `wire-tab-refresh`）**：i18n 四键 + 两张 wire 卡文案 + 定价跳转 CTA。
- **运维**：静态令牌轮换、Secret 滚动、IdP 单点风险挂账（xinru-master OOM 前科，接客前硬前提另线跟踪）。
