# Tasks — wire-self-serve-launch

## 1. 数据模型与 seed（api）

- [x] 1.1 新 alembic 迁移：`billing_customers` 加 `logto_sub` 唯一可空列、`billing_plans` 加 `is_public` 标记列；迁移后 `alembic upgrade head` 幂等可重跑，drift check 通过
- [x] 1.2 四档 seed + 旧三档禁用：free（¥0/1,000 行）、basic（¥10/100,000 行）、standard（¥50/1,000,000 行）is_public=true；starter/professional/enterprise 置 is_active=false；单测断言 seed 幂等与旧档不可指派
- [x] 1.3 公开档位端点 `GET /api/v1/plans`（仅 is_public && is_active，含月费/含额/档位码）+ 集成测试（旧三档与自定义档不出现）

## 2. 计费闭环接线（api）

- [x] 2.1 `ensure_customer()` 接入开户流：admin 开户端点（人工）与首登开户（§4）均自动代建 sub2api 账户并回写 `sub2api_user_id`；集成测试断言无手工粘贴路径仍成功、sub2api 不可达时 fail-closed 中止
- [x] 2.2 自定义档建档与指派：`POST /internal/admin/plans`（is_public=false）+ 指派校验（禁用档不可指派）；测试覆盖建档→指派→额度按新档生效
- [x] 2.3 结算与行数门口径核对：无超额档结算不产生超额账目（超额单价 0）、闭月结算金额=月费；对账单测更新

## 3. 行数门（api + registry）

- [x] 3.1 wire 额度查询端点 `GET /internal/quota/{caller}`：当月 metering_records 聚合 rows_returned vs 档位 included_lines，返回 {used, included, remaining, plan_code}；服务内测试 + 与 admin usage 聚合同源断言
- [x] 3.2 registry 预检行数门探针：到量拒绝（月度额度耗尽语义）、分钟级 TTL、探针闸门、额度服务不可达 fail-closed；余额门条件化（仅 overage>0 档检查）；registry 侧测试 + 独立开关（可先关灰度）
- [x] 3.3 端到端实弹：免费 key 消耗至 1,000 行后调用被拒（额度耗尽语义、不达上游）、免费档零余额在未到量时放行；证据存 `reports/`

## 4. 正式登录与自助开户（api + web）

- [x] 4.1 Logto UI 手工（一次性，记录到 deploy 文档）：wire SPA 应用（回调 `https://wire.finddatatech.cloud/console/callback`、CORS wire 域）+ `wire-admin` 角色 + 挂现有管理员；截图/记录入 `reports/logto-setup.md`
- [x] 4.2 api console auth 模块：`GET /api/console/auth/config` 运行时下发（未配置 configured=false）+ ID token 本地校验（JWKS/iss/aud/exp）+ sub→Customer 解析；伪造/过期令牌 401 单测
- [x] 4.3 首登自动开户触发：无映射 sub 首登→调 cell-manager ceremony（free 档跳过 cell 配给）→成功后映射生效；断点幂等重试与半开户态测试（key 铸成但映射失败→重试不重复铸）
- [x] 4.4 web 登录件平移：SPA+PKCE（抄 `fd-lex/web/src/console/auth.ts`）、sessionStorage、静默续期、登出；`LoginView` 激活、`console/customer.ts` 与 X-Customer-Id 占位退场；前端测试（配置未下发的阻塞态）
- [x] 4.5 客户侧端点身份切换：datasets/demands/provenance/ai 四路由从 X-Customer-Id 骨架切到登录映射身份，跨客户 404；路由测试更新
- [x] 4.6 免费档边界与升档：免费用户「我的数据」升级引导（无 cell 创建）、admin 升档至 basic 后 cell 配给+额度生效；端到端测试
- [x] 4.7 自助注册端到端实弹：邮箱验证→注册→首登→得 key（明文仅一次）→200 调用→suspend 即断；证据存 `reports/`

## 5. admin 双闸与占位退位（api + web）

- [x] 5.1 api admin 角色闸：`/internal/admin/*` 叠加 Logto Bearer + `wire-admin` 角色（无角色 403），X-Admin-Token 保留第二道；双闸单测（有角色无令牌拒、有令牌无角色拒、双全过）
- [x] 5.2 web admin 占位退位：token 输入框退场、导航按角色显隐、请求自动附双凭据；`AdminPages` 测试更新
- [x] 5.3 令牌轮换（运维）：fd_wire_admin_token/internal/metering 三令牌轮换 + k8s Secret 滚动 + pod 重启验证；记录入 `reports/token-rotation.md`

## 6. 用量透明（api + web）

- [x] 6.1 `GET /api/console/usage`：登录客户当月已用/含额/余量，与行数门同源；跨客户隔离测试
- [x] 6.2 控制台用量页正式化：真实数据渲染、余量低/耗尽提示（耗尽语义≠余额不足、次月恢复+升档引导）、「上线中」移除；组件测试

## 7. 定价页与站内文案（web）

- [x] 7.1 PricingView 四卡活取：档位从 `/api/v1/plans` 渲染（免费/基础/标准三卡+自定义询价卡 CTA=联系开通）、四卡响应式布局、副标题改「用完即停、次月恢复」口径；PricingView 测试重写
- [x] 7.2 i18n 清理：档位价格/行数键删除（源码无档位面量）、EN 价格符号 ¥；i18nKeys 测试通过
- [x] 7.3 首页文案更新：价值主张覆盖我的数据/上传物化/产物、删「控制台上线中」口径、双语同步；HomeView 测试更新
- [x] 7.4 docs 页计费说明与 pricingNote 联动新口径；docs 测试更新

## 8. 发布与验收

- [x] 8.1 构建与全量测试：api pytest 全绿 + web vitest 全绿 + 镜像构建（Jenkins/GHA 按 fd-wire 既有通道）
- [x] 8.2 部署滚动：api/web 滚动 + registry 探针独立 PR 灰度（先 wire 端点后探针）；生产冒烟（定价页四卡、登录流、旧三档 404）
- [x] 8.3 验收汇总：`reports/final-acceptance.md`（四档全链、行数门实弹、首登开户实弹、双闸、占位退位清单、令牌轮换记录）；`openspec validate wire-self-serve-launch --strict` 通过
