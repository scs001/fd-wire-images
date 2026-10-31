# 柏讯（wire）→ 壹座：服务账号交付与切换说明（2026-10-29）

> 承接 10-24 交接单 §2.3「舰队专属服务账号」——wire 侧已实施上线（change `add-fleet-service-accounts`）。本文档 = 交付包 + 切换指引，壹座侧读完即可行动。

## 一、交付了什么

- **Logto 专属组织** `svc-fleet`（id `kfq3i47bv5p8`），成员全部零角色、不入任何人类测试组织。
- **两个服务账号**（密码见 `docs/TEST-ACCOUNTS.md`「服务账号」节，不另贴）：

| 账号 | sub | 服务客户（billing_customers） | 用途 |
|---|---|---|---|
| `svc-fleet-core-1@finddatatech.cloud` | `ohvn98ztddjj` | `svc/fleet-core-1`（free 档） | **承接 fleet cell 的 wire-customer 链位**（即现 platform-test 所担观测主体） |
| `svc-fleet-ops-1@finddatatech.cloud` | `vsc4tv88zxhb` | `svc/fleet-ops-1`（free 档） | 运维/巡检链位备用（按需取用） |

- 两号已经真实登录探针验证可用；发号/探针/退役配方已固化：`fd-wire/scripts/provision_service_account.py`（幂等，`ensure-org`/`provision`/`retire`/`probe`/`guard`/`selftest` 六个子命令；以后要发新号跑 `provision <cell>-<n>` 即可）。
- 护栏已装机：xinru `fd-svc-guard.sh` + cron 每日 08:50——这两个号出现任何浏览器 UA 登录即告警。护栏验证不靠观察期，靠构造测试 `selftest`（临时号 tool-UA→ok / Mozilla→ALARM / 退役即停用，7/7 全绿）。
- 注：`svc-selftest-*` 命名空间为测试号（用毕即退役），请勿用作真实链位。

## 二、你们要做的：切换（收号 + 换铸币源）

1. **收号**：从 TEST-ACCOUNTS.md 服务账号节取 `svc-fleet-core-1` 的口令，入你们的密钥（别进仓/别进日志）。
2. **切换**：把 fleet cell 铸币配置里的账号口令源从 `platform-test@findmail.com` 换成 `svc-fleet-core-1@…`，然后重铸该线一次（你们 mint-line 每次全新会话、走 platform mint 回调通道——这个习惯正好合规，照跑即可）。切换后新链归服务账号名下。
3. **知会我方切换时刻**：按既定纪律，该线观测起算点自切换时刻重置（现网 platform-test 链本来 11-07 22:58 到点，切换=提前换源，不冲突）。
4. **确认周期重铸覆盖新号**：你们的每 12 天重铸自动化（`automation-4665497d`）若按行配置则自动覆盖；若按账号清单配置，把 `svc-fleet-core-1` 加进去。

## 三、切换后的红线（机理决定，务必遵守）

- **这两个号禁止任何人工交互式登录**——护栏会告警；且按 10-25 源码定案，会话销毁/同会话再授权都会杀链。铸币之外不碰它们。
- **每次铸币用全新会话**（mint-line 现状即符合），切勿复用旧 cookie 跑重铸脚本——同会话再授权换 grant = 旧链即死（14:54 链死于 22:20 同会话重铸的实证在前）。
- 改密/禁用这两个号 = 舰队链全灭（14 天墙之外新增的事故面，所以只有周期重铸能碰它们）。

## 四、切换后柏讯复核（5.2）

wire 侧只读取证确认三件事：新 grant 的 accountId=`ohvn98ztddjj`；platform-test 原有活跃链不受影响（独立会话不互杀，23:57 反例已证）；用量归属 `svc/fleet-core-1` 服务客户。复核结论回填 wire 侧 change 台账并同步你们。

## 五、需要你们回复的

1. 收到账号（一句确认即可）；
2. 切换排期（今天也行——切换不影响 platform-test 上其他线）；
3. 如需要 wire 侧配合的动作（比如先铸后切的双轨窗口），提出来我们照办。

## 六、背景一句话链

机理定案=SPA refresh token 会话绑定三条件 + 铸币起 14 天绝对租约（rotation 不续命）；周期重铸已覆盖 14 天墙；服务账号补上最后一块=人机身份解耦（归因干净、事故不连坐、计量落服务客户）。全文见 `fd-wire/openspec/changes/wire-content-fleet/reports/2026-10-24-paas-line-credential-renewal-closure.md` §八 与本 change `add-fleet-service-accounts`。

——wire 侧，2026-10-29
