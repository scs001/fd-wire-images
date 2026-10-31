# add-fleet-service-accounts — Tasks

## 1. 供给（发号配方）

- [x] 1.1 幂等建号脚本 `scripts/provision_service_account.py`（实现语言取 Python stdlib 而非 .mjs——两库都走 ssh+docker exec psql，本机零口令零依赖）：输入账号键（cell+序号）→ 查重（users.primary_email）→ DB 直插建用户（argon2i m=8192,t=8,p=1、id varchar(12) 随机、用户名同名）→ 入 `svc-fleet` org（organization_user_relations）→ 零角色断言 → 建 wire Customer（name=`svc/<key>`——`billing_customers` 无 kind 列，前缀即判别符，见 design 更正）与 logto_sub 映射——验证 = 同键跑两遍第二遍 no-op 且不轮换凭据（实测 ✓）；产出物（email/密码/sub/org/customer）打印一次并要求确认后落台账
- [x] 1.2 建 `svc-fleet` org 本身（幂等段：org 存在即复用，记录 org id 进台账）——**org id=`kfq3i47bv5p8`（tenant default）**
- [x] 1.3 发号后登录探针：headless 走一次真实登录（Logto experience API：PUT /api/experience → verification/password（identifier 形态 `{type,value}`）→ identification → submit）确认账号可用——验证 = 探针成功且审计可见该登录（UA=`fd-svc-probe/1`，护栏可见不误报）

## 2. 首批账号与台账

- [x] 2.1 发 `svc-fleet-core-1`、`svc-fleet-ops-1` 两号（含 Customer 映射）——验证 = 1.1/1.3 全过（两号探针均 ok）、org 成员恰两号、零角色（断言过）
- [x] 2.2 `docs/TEST-ACCOUNTS.md` 新增「服务账号」节（账号/密码/归属/用途/红线 + selftest-9 退役记录），org id 与 sub 在册——验证 = 表内信息与库内一致（发号即查库核对；探针成功即登录面核对）

## 3. 护栏巡检

- [x] 3.1 巡检脚本 `scripts/service_account_guard.sh`（实现取 bash——直接装 xinru 机上 cron 运行，扫 Logto `logs` 表 svc-fleet 成员 `Interaction.SignIn.Submit`）：UA 浏览器类（Mozilla*）= ALARM、工具类（fd-svc-probe/mint-line/curl）= 记录不告警——验证 = 三条探针登录样本被正确归 ok(tool) 不误报；ALARM 分支由分类逻辑覆盖（browser 前缀即 ALARM，牺牲号退役后探针失败不再产生新样本）
- [x] 3.2 每日巡检出口（已装机：xinru `/usr/local/bin/fd-svc-guard.sh` + root cron 08:50 → `/var/log/fd-svc-guard.log`，退出码=告警位）+ **护栏验证取构造测试 `selftest` 子命令（2026-10-29 用户裁决：不跑一周观察，无真实客户）**——临时号发号→工具 UA 探针→浏览器 UA 探针→guard 分类断言→即退役——验证 = selftest 7/7 ALL PASS（tool→ok(tool) / Mozilla→ALARM(browser) / 退役即停用），cron 出口装机在案

## 4. 退役路径

- [x] 4.1 停用配方（users 禁用位 + 台账行）= 同脚本 `retire` 子命令（并关服务客户 status=closed），牺牲号 `svc-selftest-9` 全链实测：发号→探针过→retire→探针拒（密码校验对 suspended 用户即拒）→护栏留痕——验证 = 禁用账号登录被拒 ✓、台账有退役行（TEST-ACCOUNTS.md）✓；链降级语义（invalid_grant→stale）以 10-25 机理定案背书（会话绑定 RT 对 suspended 主体必然失效）

## 5. 狗粮迁移协调（跨线）

- [ ] 5.1 向壹座交付：两号凭据 + 铸币切换建议（走 platform mint 通道、只铸目标账号）+ 观测起算点重置约定——验证 = 壹座确认收到并排期（交付包已备，待转）
  - **2026-10-29 进展**：交接单已转、壹座已立案（add-fleet-service-account-cutover）并回执三问两发现；柏讯侧回复见 reports/2026-10-29-paas-cutover-reply.md（口令带外直送，不进仓）。全链彩排双号 PASS（client=i0oxdswqxv4x7te99iukv、UA=mint-line/paas-1）；**新事实：consent 不免屏且每次新授权必弹，可编程批准 `POST /api/interaction/consent {}`**；①（空窗→每轮无条件重铸）②（行键=cell 属主、只换授权腿）两发现接受并改入 5.2 判据。待壹座确认收号即销。
- [ ] 5.2 壹座切换后复核：新链归属 `svc-fleet-core-1` 对应 Customer、原真人账号活跃链未受影响、用量归属正确——验证 = 审计 grant 归属 + 计量抽查

## 6. 收口

- [x] 6.1 `openspec validate add-fleet-service-accounts --strict` 绿；确认密码不出现在任何代码/日志/报告输出（只进台账）——2026-10-31 strict 绿；两枚 svc-fleet 口令全文匹配扫描 fd-wire/docs/demands/fd-industry-data 全树（跳过台账本身）= CLEAN，口令仅在册本机 TEST-ACCOUNTS.md
- [ ] 6.2 归档（openspec archive）+ 台账补记
