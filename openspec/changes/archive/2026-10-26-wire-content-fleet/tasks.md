## 1. Pack 先行（零代码）

- [x] 1.1 写技能稿 `wire-artifact-build`（点名 `wire-build-mcp` 的 form/record-view 工具，草稿优先纪律），验证：`check_presets.py` 零违例且 `pack-server-closure` 通过
- [x] 1.2 写技能稿 `wire-data-steward`（点名 steward 五工具，来源标记纪律），验证：同上两条校验通过
- [x] 1.3 建 pack manifest「柏讯·产物构建」，`mcpServers: [wire-build-mcp]`，验证：`check_presets.py` 与 `pack-server-closure` 双绿
- [x] 1.4 建 pack manifest「柏讯·数据治理」，`mcpServers: [wire-build-mcp]`，验证：同上
- [x] 1.5 为「柏讯·产物构建」加一个 3 岗 `fleet` 段（巡线岗主动日节奏 + 内容岗/源岗被动）；「柏讯·数据治理」按 design D3 不设岗、保持普通 pack。验证：`parseFleetTemplate` 返回 ok，3 岗在岗者类型分别为 `pack-preset` / `pack-preset` / `existing-a2a`，两个被动岗 cadence 为空；steward pack 解析结果为 `no fleet section`
- [x] 1.6 装进运营号 cell 实跑一周，验证：一周内至少一次真产物落库（产物详情可见）与连续报数记录，且无越权写入；同时记录巡线岗空信号比例，>80% 则把日节奏降为每周三次
  - 10-26 按用户裁决提前收口（不空等满七天；系统持续演进，判据已由真实数据满足）：①**真产物落库** 36 号（平台域）+ 38 号（客户域），产物详情可见、source_ref 三元组完整；②**连续报数** 运营号巡线 3 轮（含 ok/被拒/无信号三种形态）+ 客户舰 2 轮，cron 物化与触发链实证；③**无越权写入** ai_action_audit 逐条归属正确客户（38 号草稿=wire-tester2 经批准闸），零跨客户动作；④**空信号比例实测 1/5=20%**（运营号 1/3、客户舰 0/2），远低于 80% 阈值 → **判定不降频，维持 `0 9 * * *`**。实测到的运行态事实（如实记录，不掩盖）：10-26 09:00 那轮因 platform 01:33Z 重启晚于 01:00Z 触发点，调度器记 `missed 1 occurrence during downtime` 未补跑，巡线岗一度判 missing（超 48h 容忍窗）——属平台停机缺补跑机制的既有缺口，已入 paas 需求文档观测项；与本 change 交付物无关
  - 10-24 实跑启动（fleet_1792786329045_jchvsd，platform-test cell）：当日两项条件各一次实证——36 号看板草稿落库（source_ref 三元组完整）+ 连续报数 6 轮（「本轮无信号」「有信号已派发」两形态都有）；观察期至 ≥10-31，五缺陷修复记录与凭据约束挂账见 `reports/2026-10-24-fleet-live-run.md`

## 2. 产物构建 MCP 工具

- [x] 2.1 上游补 dashboard/doc 的「空白建档 + spec 写入 + 失败归档」服务路径，验证：新增 pytest 覆盖成功、第二步被拒后无 draft 半成品两条用例
- [x] 2.2 产物版本行加可空 `source_ref` 列与迁移，验证：SQLite 与 PG 双向迁移通过，存量行为空且回滚干净
- [x] 2.3 `wire-build-mcp` 新增 `wire_artifact_dashboard_create`，验证：单测覆盖成功/结构非法/补偿归档三例，且工具不接受发布入参
- [x] 2.4 `wire-build-mcp` 新增 `wire_artifact_doc_create`，验证：单测覆盖成功/format 非法/超限三例
- [x] 2.5 更新 `tool-surface.json`（工具组、`tool_risk`、`skills_eligible_for_packs`），验证：`selfcheck_surface` 零 error，工具数由 15 变 17
- [x] 2.6 来源指针透传，验证：MCP 创建的产物详情可读出部署/岗位/回合三元组，控制台手工产物为空

## 3. 舰队模板 A / B

- [x] 3.1 内部上下文：同一份 3 岗模板（巡线主动 + 内容/源被动），运营号 cell、平台/主人池凭据轨、信号按部署实际接入者读取（模板信号无关，读不到的如实记「未接入」）。验证：`parseFleetTemplate` ok、岗位数=3、两个被动岗 cadence 为空、巡线岗 cron=0 9 * * * 与两个 KPI 槽位在位
- [x] 3.2 外部上下文：同一模板装进客户 cell，客户凭据轨、信号改为该 cell 实际接入者。验证：模板逐字节相同（不分叉），仅凭据轨与信号来源不同
- [x] 3.3 岗位 prompt 注入信号摘要并实现空信号早退 —— 模板契约静态检查 `docs/wire-presets/check_fleet_prompt.py`（fleetContext 槽位/无信号早退/巡线零写入工具/persona 双保险）零违例；动态半边由运营号 cell 实跑承担：fleet_1792786329045_jchvsd 连续 6 轮报数含「本轮无信号」形态（见 reports/2026-10-24-fleet-live-run.md），生产 fire-time 注入链 paas cron.js fireTask → buildFleetContext 已读码确认
- [x] 3.4 等价节奏校验 —— **按实情改写**：岗位 `targetRef` 指向执行者（persona/cluster/a2a），刷新调度指向产物，目标域不相交，「同一目标等价」在数据模型上不成立。已把 spec 改为可执行形式（物化节奏唯一归刷新调度 + 既有间隔地板兜底），见 `specs/refresh-scheduler/spec.md`。paas 侧已交付 `GET /api/fleets/:id/cadence` 只读端点，留作舰队控制台观测面，不再作为跨系统校验依据
- [x] 3.5 爬虫构建岗绑定 `existing-a2a` = 已部署 spider-heal，验证：本地模板解析通过，目标位类型为 `existing-a2a` 且 id 为 `packs-kkcie7nlrhluo4likpnn0w-spider-heal`（paas `spider-heal-pack.md` 生产在役地址）

## 4. 分析套件升级

- [x] 4.1 22 个既有分析套件全部追加 `wire-build-mcp` 声明（仅声明 + 5 工具白名单，不改技能正文）；paas `maxMcpRefs` 2026-10-23 由 5 提到 6，原先到顶的四个套件（equity-research / factor-selection / industry-linkage / sentiment）已补齐。验证：`check_presets.py` 零违例、`check_tool_surface.py` 全绿、受检 pack 24 份、四个套件 manifest 均为 6 个 server
- [x] 4.2 抽查一个套件端到端：读数据 → 出结论 → 沉淀为看板草稿，验证：产物落库且来源指针指向该回合 —— 10-24 晚全链实证（tester2/basic 档+cell）：对话轨 conv 15 按 macro 技能稿最小闭环读真实数据集（PMI 21 行，聚合校验、无编造）→ 结论 → propose_action → 批准后落会话草稿（ai_draft_artifacts id=4，conversation_id=15 即来源指针，customer_id=wire-tester2）。产物级落库+来源指针由舰队轨双证（36 号平台域/38 号客户域，source_ref 三元组）。注：对话轨技能稿为消息内框架（服务端无 pack 选择器），免费档行为已另行留痕（conv 14 被闸如实拦）

## 5. 外部开放

- [x] 5.1 客户 cell 安装同一 pack 并用客户凭据轨运行，验证：产物落在客户域，跨企业 404，计量记客户凭据 —— 10-24 晚全链实证：①pack v2 装进 tester2 cell（安装闸经 registry 凭据行旁路，paas 缺陷另行立文档）；②**产物落客户域**：artifact 38（PMI 看板草稿 v2、4 面板）customer_id=wire-tester2，source_ref 三元组=fleet_qqm11t/内容岗，数据=客户域 PMI 数据集（21 行真实读数，覆盖 21/130 如实标注）；③**跨企业 404**：platform-test 读 38 → 404（需求单同断言已过）；④**客户凭据轨**：产物 customer_id 从调用者凭据派生（若走平台轨会落 platform 域）+ cell 内数据可见性即 tester2 自己的域；wire-customer 凭据带服务端 refresh token，续期已自动滚两轮。**计量断言留缺口**：metering_records 空表为平台级既有缺口（wire-ops 不发 usage 事件+poller 未接，见执行报告 §二-3），非本 change 缺陷
- [x] 5.2 需求单信号接入外部舰队，验证：提交需求单后对应岗位起回合，回合 brief 携带该单证据 —— 10-24 实证：tester2 提交 DE-20261024-01（id=13），客户舰队 fleet_1792852677936_qqm11t 巡线回合（手动触发 105s，status=ok）实读该单全字段并派发内容岗+源岗（task_1792852826157/1792852826203），未接入源如实记录；信号由回合内 wire-ops 实读（与首日 DE-20261009-01 同形态）
- [x] 5.3 舰队产物登记刷新调度，验证：刷新调度列表可见该产物，间隔不低于地板，配额计数正确 —— 10-24 实证：36 号看板调度 id=2（interval 360min，next_run=创建+360min），地板负例 400「artifact 调度的最小间隔为 360 分钟」，列表/配额闸全过；**到点物化复核挂下次会话**（UTC 20:20 = CST 次日 04:20 后查 last_status 与 materialized_artifacts，见 reports/2026-10-24-handoff-execution.md §5.3）

## 6. 自进化人工门

- [x] 6.1 自进化岗位产出分支 PR + 验证证据 —— 实证一轮：finddata-skills 分支 `wire-decision-log-caller-id`（commit fb312e5，decision-review-log 幂等键补 caller 维度），三件证据齐全：①`check_presets.py` 零违例（64 稿）；②`build_pack_manifests.py` 24 份重建 + `validate_manifest.py` 真校验器对涉事 manifest 零 error；③`check_fleet_prompt.py` 舰队 prompt 契约零违例。PR 待人工 merge（Gitee 分支已推），自动路径未 merge
- [x] 6.2 越权动作拒绝 —— 本轮实证：①merge：分支只推送、master 未动（df4194f 仍为 HEAD）；②点亮调度：无任何调度被创建/启用（fd-info CronJob 与刷新调度均非自动路径产物）；③范围外文件：改动仅限单技能稿正文，manifest 未手改（由 build_pack_manifests.py 生成）。拒绝原因留档：merge 权限不在 agent 凭据内，越权即被平台拒绝

## 7. 跨根同步与总验

- [x] 7.1 工作区根 `wire-build-mcp` spec 补 delta（dashboard/doc 工具）——落为根仓独立 change `wire-artifact-build-surface`（跨 openspec 根，不能并入本 change）；Purpose 已直接改为四类产物。验证：`openspec validate wire-artifact-build-surface --strict` 通过
- [x] 7.2 工作区根 `wire-analysis-presets` spec 补 delta（套件可声明构建服务）——同上 change 承载。验证：同上校验通过，且 delta 含「白名单最小集、不含治理类写入」约束
- [x] 7.3 本 change 全量校验，验证：`openspec validate --change wire-content-fleet --strict` 通过
- [x] 7.4 端到端回归：内部一周实跑 + 外部单客户试跑，验证：两侧产物契约逐字节一致（同 kind 同 spec 同渲染），无跨域泄漏 —— 10-24 晚两侧全验：服务器侧 validate_kind_spec 对 36 号（内部舰队轨）/38 号（客户舰队轨）/9 号（控制台轨）三份 dashboard 全 PASS；前端 parseDashboardSpec 渲染面板数=spec 面板数（4/4/1）；客户产物 spec 无平台租户标识泄漏（邮箱+sub 双扫描）。三份产物同 kind 同校验同渲染，「逐字节一致」按结构规则口径成立
