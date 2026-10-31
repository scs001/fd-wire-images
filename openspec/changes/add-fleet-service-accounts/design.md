# add-fleet-service-accounts — Design

## Context

- 杀链机理已源码级定案（2026-10-25，见 fd-wire `wire-content-fleet/reports/2026-10-24-paas-line-credential-renewal-closure.md` §八）：SPA refresh token 绑定铸币会话（三条件），同会话再授权/会话销毁即杀链；独立会话的新授权不杀链。人类「正常登录」不威胁别的链，但审计无法归因、计量混账、改密/禁用连带杀链。
- 壹座侧铸币已是 headless（`mint-line/1`，走 platform mint 回调通道），14 天墙由其周期重铸自动化覆盖（每 12 天）——**服务账号不解决 14 天墙，解决的是身份归属与 人/机解耦**。
- 既有资产：Logto 1.42.0（xinru-master docker）；建号/改密 DB 直插配方在册（`docs/TEST-ACCOUNTS.md` §五/§六：argon2i m=8192,t=8,p=1、id varchar(12)、首登 Set username 坑）；wire 侧身份模型 = Logto sub ↔ Customer 映射（console-auth spec）；org 表可用（`wire-testers`=pdr0vgxmcem6 先例）。
- 用户裁决（2026-10-29）：现在就做；现役所用均为测试账号，测试账号集可直接用。

## Goals / Non-Goals

**Goals:**
- 一套可重复、幂等的服务账号发号配方（v1 脚本化，5.x 再端点化）
- 人/机在租户内组织级隔离 + 命名可辨识
- 交互式登录可巡检、可告警
- 退役路径与既有 stale 语义对接
- 狗粮迁移：现役舰队凭据切到服务账号，不干扰其他活跃链

**Non-Goals:**
- 不做 Logto M2M client_credentials 改造（真机器身份动静大，5.x 另议）
- 不改续期/重铸机制与节奏（壹座侧既有）
- 不做发号 HTTP API / 控制台 UI（狗粮阶段脚本即可）
- 不动 `wire-testers` 三号的既有用途与档位
- 不新增 14 天墙的对付手段（已由壹座自动化覆盖）

## Decisions

1. **v1 建号通道 = DB 直插脚本（复用既有配方），非 Management API**
   - 理由：配方已在册且被 p2walk1025 等多次验证；Management API 需 M2M 凭据与租户配置，狗粮阶段收益低。
   - 备选（否）：Management API 端点化——留作 5.x 方向（届时外部客户 cell 自动发号才需要）。
2. **组织隔离：新建 org `svc-fleet`，账号零角色**
   - 理由：org 是租户内现成的分组边界；零角色同时避开「role 与 registry 组补全互斥」坑（服务账号不需要 registry 工具面）。
   - 备选（否）：仅靠命名前缀区分——清单与巡检都难精确圈定集合。
3. **命名约定 `svc-<cell>-<n>@finddatatech.cloud`，用户名同名**
   - 首批（狗粮）：`svc-fleet-core-1`（承接现 platform-test 的 fleet 链位）与 `svc-fleet-ops-1`（承接 admin 巡检用链位）；账号键即 cell 标识 + 序号。
4. **凭据归属：每服务账号映射一个 wire Customer，命名前缀 `svc/` 承载 kind**
   - 理由：复用 sub ↔ Customer 单一真源；计量/配额天然按服务客户归属。Customer 命名 `svc/<cell>-<n>`。
   - 实施更正（2026-10-29）：`billing_customers` 无 `kind` 列——原 design 写的 `kind=service` 落地为 **name 前缀 `svc/` 约定**（唯一索引在 name 上，前缀即判别符），plan 挂 free。
5. **密码保管：只进 `docs/TEST-ACCOUNTS.md`「服务账号」节 + cheap-1 令牌文件**
   - 沿既有敏感文件约定（内部流转、不进公开仓、不贴代码/UI）；与测试账号同管线，不新造密钥设施。
6. **护栏 v1 = 巡检脚本扫 Logto 审计 `logs` 表**
   - 判据：svc-fleet 成员的 `Interaction.SignIn.Submit`，按 UA 分类——浏览器 UA（Mozilla*）= **ALARM**；工具 UA（`fd-svc-probe/1` 探针、`mint-line/1` 铸币、curl 等）= 记录不告警。探针登录会进审计（可见可核），属预期。
   - 实施形态：`scripts/service_account_guard.sh` 装机 xinru `/usr/local/bin/fd-svc-guard.sh` + root cron 08:50 每日（26h 窗口）追加 `/var/log/fd-svc-guard.log`，退出码=告警位（可接告警通道）。验证门按用户裁决（2026-10-29，无真实客户不跑观察）改为构造测试：脚本 `selftest` 子命令——临时号全链（tool UA→ok / browser UA→ALARM / 退役即停用），绿即视为护栏验证通过。
7. **退役 = Logto 禁用（users 表禁用位）+ 台账行**
   - 链的死法自动落既有语义：续期 `invalid_grant` → 行 stale、提示重连（壹座已实现），无需 wire 改动。
8. **迁移纪律：切换即知会壹座重置该链观测起算点；只铸目标账号，不动其他活跃链**
   - 机理背书：独立会话铸币不互杀（23:57 反例已实证）。

## Risks / Trade-offs

- [DB 直插绕过 Logto 应用层，字段漂移风险] → 脚本断言关键字段（id 长度、密码哈希参数、org 关系行）+ 建号后即时登录探针验证。
- [服务账号密码泄露面与测试账号同管线] → 数量最小化（首批 2 个）、命名隔离、巡检告警；5.x 端点化时换密钥库。
- [巡检 UA 判别可能漏报/误报] → 判据保守（只报浏览器 UA 的登录），漏报代价=审计缺失而非安全洞；误报进报告不阻断。
- [壹座侧切换排期不受本仓控] → wire 侧交付账号+配方即算闭环，切换时间由壹座定，切换前后双轨可用。

## Migration Plan

1. 建 org + 首批两号 + Customer 映射（脚本，幂等）→ 登录探针验证 → 台账。
2. 巡检脚本上线（只读）→ 并行观察一周无误报。
3. 知会壹座：账号交付 + 铸币切换建议（走 platform mint 通道）+ 观测重置约定。
4. 回滚：删服务账号 Customer 映射 + 禁用账号即可；不影响任何现役链（未切换前零耦合）。

## Open Questions

- `svc-fleet-core-1` 承接 fleet 链的具体切换时点（等壹座排期；不阻塞本 change 验收）。
