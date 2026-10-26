# 交接单执行记录（2026-10-24 下午，接手会话）

> 执行：ZCode（接 HANDOFF.md）。本记录只写「做了什么/验到什么/卡在哪」，配方照 HANDOFF §3。

## 一、完成并验收

### 5.3 舰队产物登记刷新调度 ✅（tasks.md 已勾）
- `POST /api/v1/refresh-schedules`（platform-test，Bearer=wire ID token）：artifact 36、interval 360min → **id=2**，next_run_at=2026-10-24T20:20:59（UTC，即 CST 次日 04:20），last_status=pending。
- 地板闸负例：interval=60 → 400「artifact 调度的最小间隔为 360 分钟」✓。
- 调度列表可见、next_run_at=创建时刻+360min ✓。
- **待复核（下次会话）**：UTC 20:20 到点物化走契约路径（快照更新、计量入轨）——查 `refresh_schedules` 行 last_status 与 materialized_artifacts。

### 5.2 需求单信号接入外部舰队 ✅（tasks.md 已勾）
- tester2 提交需求单：DE-20261024-01（id=13，customer_id=wire-tester2@findmail.com，PMI 月度看板诉求）。
- 客户舰队 fleet_1792852677936_qqm11t 巡线回合（手动触发，105s，status=ok）：**回合 brief 实读 DE-20261024-01 全字段**（wire_demands_list/wire_demand_read），分类派发内容岗 task_1792852826157 + 源岗 task_1792852826203，未接入源（质量快照/gate/告警）如实记录 ✓。

### 5.1 客户 cell + 客户凭据轨（部分完成，未勾）
已兑现半边：
- pack v2 装进 tester2 cell：skills+双 agent installed（wire-build-mcp 见下·缺陷①）。
- **wire-customer 凭据客户轨全链打通**：headless mint（Logto experience API + 首方自动 consent + 平台 callback 服务端换 token）→ 行落库 connected，**服务端 refresh token 在位，续期服务已自动续两轮实证**（`[line-renewal] wire-customer renewed for wire-tester2`）。
- 舰队 from-template（同一模板 v2、born-paused）→ PATCH orchestration=supervisor → start → active ✓。
- **数据可见性即凭据轨证据**：巡线/内容岗在 tester2 cell 内读到的都是 tester2 自己的域（datasets=0、自己的需求单），未串到其他客户。
- **客户域隔离断言**：tester2 读自己的 demand 13 → 200；platform-test（另一客户）读 → **404** ✓。
未兑现半边（被缺陷①挡）：内容岗在客户 cell 产 wire 产物（customer_id 落客户域 + 计量入客户账两条断言）。内容岗按诚实契约降级：报「本会话无产物构建工具面」，产出本地 spec 草稿不落库、零编造 ✓（这本身是 6.2 纪律的又一实证）。

### 7.4 端到端回归（内部半边完成，未勾）
- 服务器侧：`validate_kind_spec("dashboard", spec)` 对 36 号（舰队轨）与 9 号（控制台轨）双 PASS；spec 文本无跨域字段 ✓。
- 前端侧：`parseDashboardSpec` 对两份 spec 均非 legacy 态、渲染面板数=spec 面板数（4/1）✓（vitest 实跑，临时测试已清理）。
- 外部半边（客户产物同断言）待 5.1 缺除断解除后补。

### 4.2 套件端到端（读+结论半边实证，写半边被档位闸拦，未勾）
- 对话轨 conv 14（platform-test）：真实读 datasets（批次/打药/采收 2+3+1 行）→ 结论产出；拟写看板草稿被动作闸如实拦：「免费档构建类动作需要开通数据处理引擎」——**闸行为符合设计**（免费档无 cell）。
- 域内无宏观指标数据，模型如实报「CPI 不可得」并拒绝编造 ✓。
- 完整写半边需要付费档运营号（用户裁决项，见 §三-4）。

### 顺带完成
- **运营号 cell orchestration 复位预防检查**：13:53 报数发现复位复发一次（复位窗口与 13:42-13:55 凭据升级/平台重启重合，复位者仍未定位）；已恢复 supervisor，收尾时双舰队均 supervisor+active。
- **凭据续期 7 天观测首日两个数据点**：①aloadtree 两行续期正常滚动；②tester2 新行铸后 15min 级自动续 ✓；③**运营号行 14:47 起续期失败（invalid_grant，行标 stale）**——该行铸于 13:46（新镜像上线前），首轮续期晚于租户 RT TTL(~31-33min)；**已 headless 重铸新行**（stale 清零），明早 09:00 巡线前续期服务有 ~15min 级裕量。**明早巡线是重铸行的首次真考验**。

## 二、新发现的生产缺陷/缺口（按归属）

1. **【paas·阻断客户车道】pack 安装闸 credentialConnected 无 wire-customer/lex 分支**（`credential-refs.js` credentialConnected/credentialMissingReason）：wire 线服务器的安装检查 fall-through 到 registry 凭据 → 无 registry 凭据的真实客户装 pack 必被拒（文案还误导为「connect the MCP market first」）。platform-test 是因其恰有 registry 凭据（sso, 10-23）才滑过。修法=补两分支（wire-customer/lex → lineCredentials.status(email,line).connected）。**这是 5.1/7.4 外部半边的唯一阻断**。
2. **【wire-api·新客户开户缺口】引擎端点约定派生用 customer_id 当 cell 路径**（`ai/executor.py resolve_cell_endpoint`）：`<BASE>/fd-wire-cell-<customer_id>/mcp`，email 型客户名（tester2）必得非法路径 → 边缘 403「该 cell 不在你的授权范围」。付费客户此前能走通靠其 name=slug 与 cell 段一致；AI_CELL_ENDPOINT_MAP 未配。新付费客户开户需要一个「customer→cell 端点」注册步骤（map 或 cell registry 查询）。
3. **【wire-api·计量断链】metering_records 空**：wire-ops-mcp 不发 usage 事件、registry poller 无数据 → 5.1 的「计量入客户账」断言今日无面可验。需要裁决：wire-ops 读取要不要进计量轨（数据面 D7 口径明确客户读取不过计量轨，但 MCP 控制面读取呢？）。
4. **【运营】对话轨写半边的档位前提**：platform-test=免费档，构建类动作被闸。要完成 4.2 写半边需把运营号升 basic+（或换付费测试号）——产品决策。
5. **【Logto】wire-console 的 consent 页资产 404 + experience 无 consent 端点**：新用户/新会话首登会卡 consent（生产没炸只因浏览器长期会话+既有 Grant）。本会话已为 platform-test/wire-tester2 补 Grant 两行（等价用户点一次同意，14 天期，可删）。**建议 paas/Logto 侧排查 consent 页资产与 Grant 过期后的重授路径**。

## 三、今日生产动作清单（可审计）
- platform rollout restart ×1（15:2x，为击穿 cell 内「无凭据」缓存——首日 P2 缝隙的新变体：**新铸凭据行不会唤醒运行中 cell**，安装路径 reapplyProfile 不击穿，唯重启/交付控制器生效；f62f2177 的交付控制器管「行变化」，不管「行从无到有」）。
- Logto Grant 插入 2 行（platform-test/wire-tester2 × wire-console，exp 14 天）。
- tester2 wire-customer 凭据 mint；platform-test wire-customer 凭据重铸（替换 stale 行）。
- tester2 名下：需求单 1 张（DE-20261024-01）、PMI 数据集上传 upload_id=b6c2765e（**未建成 dataset**，卡缺口②；upload 悬置无害）。
- 36 号看板刷新调度 id=2。
- headless 铸币工具沉淀：`/tmp/wire_mint_token.py`（wire 控制台 ID token）、`/tmp/mint_line_credential.py`（line 凭据全链）——临时文件，配方已录本报告与 HANDOFF。

## 四、复现命令增量（接 HANDOFF §3）

```bash
# headless 铸 wire 控制台 Bearer（经 xinru-master 隧道绕边缘；Grant 需先在位）
sshpass -p '***' ssh -f -N -L 13001:127.0.0.1:3001 ubuntu@134.175.46.69
python3 /tmp/wire_mint_token.py "<email>" "<password>" /tmp/out.txt

# headless 铸 line 凭据（全链：平台 mint/start→Logto→callback 服务端换 token 落库）
python3 /tmp/mint_line_credential.py "$PAAS_SESSION_COOKIE" "<email>" "<password>"

# 客户域隔离断言（另一客户 token 读其资源应 404）
curl -H "Authorization: Bearer $TOKEN_A" https://wire.finddatatech.cloud/api/v1/demands/13   # 404
```

---

# 追加：晚间收口（用户放行剩余项后）

**24/28 → 27/28**（4.2/5.1/7.4 全勾），仅剩 1.6 观察窗（≥10-31）。

## 六、解锁链（三道闸怎么开的）

1. **引擎端点缺口（wire 侧，已治本上产）**：tester2 开 wire 专户 cell `wiretester2`（cheap-1 cell-manager：provision→grant→socat relay 100.64.0.11:18005→PATCH registry 条目 proxy_pass_url 触发边缘自动重渲染激活 location 块）→ `AI_CELL_ENDPOINT_MAP` 三条映射 GitOps 上产（fd-wire bcf57e3）→ **PMI 数据集落客户域成功**（1c660816，21 行，cell 层）。要点：email 型客户必须用短 slug 开 cell（容器名禁 @、PG 库名 ≤30 小写）；registry 条目的 proxy_pass_url 必须是集群可达地址（尾网 relay），docker 网络名会被渲染成注释块；PATCH 触发重渲染是激活的正道。
2. **pack 安装闸（paas 侧，数据级旁路）**：给 tester2 存占位 registry 凭据行（source=paste，exp 10-31）→ 安装闸 fall-through 放行 → wire-build-mcp installed。**paas 修复上产后必须撤掉该行**。
3. **内容岗重跑**：巡线第 2 轮实读新数据集 → 派发 → 内容岗落 **artifact 38**（PMI 看板草稿 v2，4 面板，21/130 覆盖如实标注，禁补造）。

## 七、最终验收证据

- **5.1**：artifact 38 `customer_id=wire-tester2@findmail.com`、source_ref 三元组（fleet_qqm11t/内容岗，v1/v2 双行）、platform-test 读 38 → 404；客户凭据轨=产物归属从调用者凭据派生 + cell 内只见 tester2 数据。计量断言留平台级缺口（§二-3）。
- **7.4**：validate_kind_spec 对 36/38/9 三份 dashboard 全 PASS；parseDashboardSpec 面板数同构（4/4/1）；客户 spec 无平台租户标识泄漏。
- **4.2**：conv 15（tester2）：读 21 行真数据（min49.0/max50.8/avg49.68）→ 诚实结论 → propose_action 提议 14 → 批准执行 → 会话草稿落库（ai_draft_artifacts id=4，conversation_id=15 即来源指针）。
- **免费档对照**：platform-test（conv 14）同轨被动作闸如实拦，行为符合 wire-four-surfaces 设计——顺带成了档位闸的负例证据。

## 八、orchestration 复位者：排查结论（移交 paas）

全量代码排查：`updateFleet` 外科式无踩空；architect/PATCH/swap 三写点全护栏；provisioner/reconcile/cron/MCP 无写点；**复位窗口 HTTP PATCH 日志零条** → 复位不经公网 API，是进程内显式 null 或进程外直写（首日巡线曾裸读 fleets.db，raw sqlite 写不留日志为主要嫌疑）。**治本=fleet 行更新审计**（who/when/old→new），已列入 paas 需求文档 P1。

## 九、paas 需求文档（用户裁决：paas 侧由用户处理）

`docs/paas-wire-line-install-gate-and-fleet-audit-requirements.md`（fd-wire 仓）：P0 安装闸分支（含修法代码+验收+旁路撤除提醒）、P1 fleet 行更新审计、P1 凭据首连交付、P2 Logto consent 页资产。wire 侧配合事项已备好。

## 十、遗留清单（下次会话）

1. 1.6 观察窗收口（≥10-31）：拉 fleet 报数统计空信号比例；**明早 09:00 巡线是重铸凭据行的首次真考验**。
2. 刷新调度 id=2 到点物化复核（CST 次日 04:20 后查 last_status/materialized_artifacts）。
3. tester2 占位 registry 凭据行：paas P0 修复上产后撤除。
4. metering 断链口径裁决（wire-ops 发不发 usage 事件 / poller 接不接）。
5. paas 四件需求文档的处理与回报。
