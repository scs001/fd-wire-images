# 柏讯→壹座：凭据切换（add-fleet-service-account-cutover）配合事项回复

日期：2026-10-29　回复方：柏讯（wire）侧　针对：壹座侧切换立案回执三问两发现

## 一、收号通道：gitee 拉取不可行，本单即带外通道

- `docs/TEST-ACCOUNTS.md`（凭据总表）位于 finddata 根 `docs/`，**该目录不在任何 git 仓内**（fd-wire 的 gitee 仓只含 `fd-wire/` 自身），故「fetch/pull gitee 该提交为准」取不到服务账号节——这是红线设计：口令永不进版本库。
- 带外直送：两号口令已随本转发单单独送达（另附一页，**阅后请勿贴回任何仓/工单/日志**）。邮箱即用户名：`svc-fleet-core-1@finddatatech.cloud`、`svc-fleet-ops-1@finddatatech.cloud`。
- 若切换窗口内口令遗失，柏讯侧随时可用 `scripts/provision_service_account.py` 重设（同键幂等、原地换 hash），不阻塞排期。

## 二、排期确认

11-01～11-05 单次原子切换：**确认，无需双轨**。platform-test 旧链切换后自然闲置，其 14 天绝对租约壁钟在 11-07 22:58，与切换窗口无冲突；旧链无需任何处置。

## 三、3a 彩排结论：可登录 ✓，但**首连非免屏**（consent 每授权必弹，可编程批准）

柏讯侧已代跑全链彩排（**含 ops-1，省得你们再跑一次**），client=`i0oxdswqxv4x7te99iukv`（wire-console SPA）、`scope=openid offline_access`、redirect `https://wire.finddatatech.cloud/console/callback`、UA=`mint-line/paas-1`、Logto 主机 loopback：

| 账号 | 登录 | 授权 code | offline_access RT 发放 | refresh 轮换 |
|---|---|---|---|---|
| svc-fleet-ops-1 | ✓ | ✓ | ✓ | ✓ |
| svc-fleet-core-1 | ✓ | ✓ | ✓ | ✓ |

（即弃彩排：只验授权面，token 用完即弃，未落任何持久行。）

**关键事实：consent 不免屏。** 提交登录凭证后 submit 的 redirectTo 会 302 到 `/consent?app_id=…`；且实测**每次新的授权交互都会再弹**（第二遍带 `prompt=login` 重跑仍到 consent 屏——consent 不按用户记忆，wire-console 非第三方应用也没有跳过开关）。两种处理方式，任选其一即可，均经实测/源码核实：

1. **浏览器流（playwright，即你们的 paas-1 形态）**：在 consent 页点「授权」按钮即可——你们既有流程天然吸收这一步，无需改动。
2. **纯 API 流**：对 `/consent` 重定向所在交互直接
   `POST /api/interaction/consent`，body `{}`（同交互 cookie 会话），返回 `redirectTo` 后跟随即得 callback `code`。源码依据：Logto 1.42 `packages/core/src/routes/interaction/consent/index.ts`。

补充两点：refresh 轮换**不**重弹 consent（仅初始授权要批一次）；若贵方希望彻底免屏，需在 wire-console 应用配置层面动 consent 开关，影响全部真人用户登录面——贵方立案再议，柏讯侧不单方面改。

**结论：mint-line 把「登录→授权页批准→code→token」当固定四步跑即可，首连不会被拦死。**

## 四、3b UA 约定：接受，零改动

`mint-line/paas-1` 匹配工具类特征（护栏 ALARM 规则只认浏览器 UA `Mozilla*`），已实测归 `ok(tool)` 不误报。ops-1/core-1 每次无人值守登录都会在柏讯侧每日护栏日志留痕一行；一旦贵方误用浏览器 UA（或真人手工登这两个号），次日报警——这是约定生效的自检机制，非阻塞。

## 五、两项发现回执（均接受）

- **①（12 天检查 + 临期 <36h 触发的稳态空窗）**：接受，「每轮无条件重铸」与 14 天绝对租约机理自洽（轮换继承剩余寿命，唯一硬顶=首次 mint+14d；无条件重铸把壁钟永远清零）。柏讯侧 5.2 复核口径同步按此。
- **②（行键与授权主体两层）**：接受并记入复核清单——凭据行键保持 `platform-test@findmail.com`（cell 属主）不变、只换 Logto 授权腿为 `svc-fleet-core-1`，属预期形态；5.2 判据相应为「新 grant `accountId=ohvn98ztddjj` + 行键未动」双条件，不再以行键判归属。

## 六、切换后柏讯侧复核清单（5.2）

1. 新授权链 grant `accountId=ohvn98ztddjj`（core-1），且 RT 会话绑定语义与真人链一致；
2. 原真人账号（platform-test、aloadtree 线）活跃链零受影响（对照基线：10-29 在册 grant 集合）;
3. 用量计量归属 `svc/fleet-core-1` 服务客户行；
4. 护栏日报：切换日起 mint 时段出现 `mint-line/paas-1` 类 UA=正常，出现 `Mozilla*`=ALARM 当日通知贵方。

——柏讯侧　add-fleet-service-accounts / 5.1 交付回复
