# 交接：wire-content-fleet 收尾（2026-10-24）

> 交接人：Claude（本会话）。接手人：ZCode。
> 状态：**22/28**。剩 6 项，全部要真 cell / 真用户轨 / 观察窗，不适合在本机模拟。
> 本文给出每项的现状、做法与验收口径，接手即做，不用重读 change 全史。

## 0. 全局事实（免重查）

- change 位置：`fd-wire/openspec/changes/wire-content-fleet/`（fd-wire 根，非工作区根）。
  状态推进用 `cd fd-wire && openspec instructions apply --change wire-content-fleet --json`。
- 已上产：`sha-8b6a68b`（source_ref 列 0040 迁移已 stamp、dashboard/doc 两步创建、证据门 warn、info 面板契约）。
- 舰队已在跑：运营号 platform-test@findmail.com cell（`3fdc7b8e8ac03b68`），fleet id `fleet_1792786329045_jchvsd`，巡线 cron `0 9 * * *`。首日实录 `reports/2026-10-24-fleet-live-run.md`（五缺陷全修，含凭据续期 paas 当日闭环）。
- pack：`wire-artifact-build` v2 已发（`SWPvacoFTftfVngMMFotwQ` v2，2026-10-24；v1=`AjFHQ9tjiJp5GTYmBC2y8g` 是早期公开件，市场里两个并存是预期的——安装用 v2）。`wire-data-steward` v1（`8edSwnv3lIXcuSAkzS9vvA`）。
- 22 套件已全量带 `wire-build-mcp` 声明（v2 重发过一轮）。
- 自进化 PR：finddata-skills 分支 `wire-decision-log-caller-id`（fb312e5）已推 Gitee，**待人工 merge**（Gitee 网页建 PR 即可，本地 master 未动）。
- 新增校验器：`docs/wire-presets/check_fleet_prompt.py`（舰队 prompt 契约：fleetContext 槽位 / 无信号早退 / 巡线零写入），已入 `check_presets.py` 同级，跑批时一起跑。
- fd-wire 工作树：干净（ZCode 的 docker 修复等已在最近提交里）。工作区根 `finddata-skills` 有一处未提交的 `decision-review-log/SKILL.md` 改动——那是 **PR 分支的同款内容在 master 上的残留**，merge PR 后用分支内容覆盖即可，别误回滚。

## 1. 剩余六项怎么做

### 1.6 运营号 cell 实跑一周（跑动中，D1=2026-10-24）

- 什么都不用做，等 ≥10-31。验收两条：①≥1 次真产物落库（已有：36 号看板，10-24）；②连续报数 + 无越权写入。
- **到点后的动作**：拉 fleet 报数记录（`GET /api/fleets/fleet_1792786329045_jchvsd/reports` 或 cockpit），统计巡线「本轮无信号」比例；**>80% 则把巡线 cron 降为 `0 9 * * 1,3,5`**（每周三次，改 fleet 成员 cadence 即可，落的是配置不改契约），并把比例记进 tasks.md。
- 顺带盯凭据续期 7 天观测（paas 侧闭环报告要求 10-24 起算）：巡线 09:00 回合读面应持续 200、零浏览器操作。异常 = 回合报「未接入」→ 查 platform 日志 `line-credential` 续期分支。

### 4.2 套件端到端抽查（读数据 → 结论 → 看板草稿 + source_ref）

- 产物落库半边已被内容岗真跑实证（36 号看板 source_ref 三元组完整）；**缺的是"用分析套件技能稿走一遍"的完整链**。
- 做法（conversation 轨，不必走舰队）：
  1. 运营号登录控制台 → AI 构建对话；
  2. 让它按 `macro-cycle-analysis` 技能稿跑一个最小问题（如"取 CPI 最近 12 期并总结趋势"）；
  3. 结论出来后让 AI 调 `wire_artifact_dashboard_create` 落草稿，**带 source_ref**（conversation 轨没有 fleet 身份行 → 允许不传，验收只看舰队轨的 source_ref，所以更简单的替代：直接复用 36 号看板作为本项证据，把"技能稿驱动"这半边补一次对话实录即可）；
  4. 验收：产物详情可见 + `GET /api/v1/artifacts/{id}/versions/1` 的 `source_ref` 指向 fleet 三元组（若走对话轨则注明"非舰队来源，留空为预期"）。
- 我建议的最短路径：**用 36 号看板 + 一次 macro 技能稿对话实录合成证据**，别再造新看板。

### 5.1 客户 cell 同 pack + 客户凭据轨

- 前置：需要一个**真实或测试客户账号**的 cell（不是 platform-test）。若无现成客户，注册一个 basic 档测试号即可。
- 步骤：客户登录 → 装同一 pack v2 →（有数据处理引擎则开）→ 舰队 from-template → start → 手动触发一轮内容岗 → 核三件事：
  - 产物 `customer_id` = 客户（落在客户域）；
  - 平台租户 `GET /api/v1/artifacts/{id}` 对该产物 **404**（跨企业隔离）；
  - 计量：该回合读取走**客户凭据轨**（wire 入口审计里 caller=客户凭据，行数入客户账）。
- 注意：客户 cell 若无 wire-customer 凭据，先走 mint（凭据续期机制已上线，无人值守安全）。

### 5.2 需求单信号接入外部舰队

- 客户 cell（5.1 的）提交一张真实需求单（`POST /api/v1/demands`，内容随便但要合规过闸——避开淘宝/拼多多九域）。
- 验收：巡线下一回合（或手动 `POST /api/cron/:taskId/run`）的 brief/prompt 里带该需求单证据（fleetContext 或工具读面出现 DE 单号），派发边放行。
- 若 fleetContext 只含下级报数、不含需求单：这是**预期内**的——信号由巡线用 wire-ops 工具在回合内实读（首日实录 DE-20261009-01 就是这么读到的），断言看回合 transcript 而不是 prompt。

### 5.3 舰队产物登记刷新调度

- 给 36 号看板（或 5.1 客户产物）登记刷新调度：`POST /api/v1/refresh-schedules`，`intervalMinutes ≥ 360`（地板，低于会被拒）。
- 验收：调度列表可见该产物、下次执行时间正确、到点物化走契约路径（快照更新、计量入对应凭据轨）、配额计数 +1。
- 顺带验证 3.4 改写的 spec 语义：岗位节奏没有承担物化（巡线 cron 依旧只判信号）。

### 7.4 端到端回归（内部一周 + 外部试跑都完成后做）

- 内部：拿 36 号看板 spec 全文；外部：拿 5.1 客户产物的同 kind spec。
- 断言：**同 kind 同校验同渲染**——两份 spec 都过 `validate_kind_spec`、前端 `parseDashboardSpec` 渲染面板数一致、无跨域字段（外部产物里不出现平台租户 id）。
- 产物契约逐字节一致指"结构规则一致"，不要求内容相同。
- 全部通过后：勾 1.6/4.2/5.1/5.2/5.3/7.4 → `openspec archive wire-content-fleet -y`（在 fd-wire 根跑）。

## 2. 挂账与知会（不在六项内，别丢）

1. **`orchestration` 被复位为 null**（首日缺陷④，未定位复位者）：fleet reconcile 或 pack 相关路径疑重写 fleet 行。复发症状=巡线连被拒。修复=`PATCH /api/fleets/:id {"orchestration":{"mode":"supervisor"}}`。**建议观察期内每次巡线前查一次该字段**，抓到复位时机就能定位。
2. **源岗 vacant**：spider-heal A2A 在萬星运营号 private pack，platform-test cell 目录解析不到。巡线会如实升级而非假装派发。修复方向=把 spider-heal pack 对 platform-test 可见，或换一个该 cell 可达的 a2a 目标位（tasks 3.5 的 V1 验证首答=不通过，属已知）。
3. **paas `test-wire-tool-risk` 红**：paas 的 vendored wire 工具面快照没吸收 17 工具新版。修复=paas 跑 `scripts/refresh-wire-tool-surface.mjs`（paas 侧已挂账，wire 侧无动作）。
4. **凭据续期 7 天观测**（paas 验收①）：与 1.6 同窗，到 10-31 一并回报 paas 台账。
5. **pack 双版本并存**：市场里 `AjFHQ9tjiJp5GTYmBC2y8g`（v1，作者 platform-test）与 `SWPvacoFTftfVngMMFotwQ`（v2，作者 aloadtree）同名。发版必须**按市场作者精确匹配**（限流 10/小时/作者）。是否合并/下线旧件，产品决策，未定。
6. **`wire-indicator-to-dataset`**（fd-wire 根新 change，ZCode 已建 proposal）：依赖工作区根 `business-mcp-indicator-series-read` 先行。与本 fleet change 无阻塞关系，排期时注意依赖方向。

## 3. 快速命令（照抄）

```bash
cd /Users/chengsishi/finddata/fd-wire
openspec instructions apply --change wire-content-fleet --json | jq .progress

# 校验批（改技能稿/模板后必跑）
python3 ../docs/wire-presets/check_presets.py
python3 ../docs/wire-presets/check_fleet_prompt.py
python3 ../docs/wire-presets/build_pack_manifests.py
python3 ~/.agents/skills/facet-pack-builder/scripts/validate_manifest.py ../docs/wire-presets/pack-manifests/*.json

# 发 pack（作者匹配市场既有者；限流 10/h）
export FACET_INTERNAL_TOKEN=$(kubectl --context cheap -n fd-prod get secret facet-secrets -o jsonpath='{.data.FACET_INTERNAL_TOKEN}' | base64 -d)
python3 ~/.agents/skills/facet-pack-builder/scripts/publish_pack.py \
  ../docs/wire-presets/pack-manifests/wire-artifact-build.json \
  --author aloadtree@gmail.com --pack-id SWPvacoFTftfVngMMFotwQ

# 运营号 cell 会话铸币（fleet API 用）
SECRET=$(kubectl --context cheap -n fd-prod exec deploy/platform -- cat /data/auth/session-secret)
# node mint-cookie.mjs "$SECRET" "platform-test@findmail.com" "admin,wire-customers,creators"

# 恢复派发边（orchestration 复位时）
# PATCH /api/fleets/fleet_1792786329045_jchvsd {"orchestration":{"mode":"supervisor"}}

# 手动触发巡线
# POST /api/cron/:taskId/run   （taskId 从 cockpit / GET /api/fleets/:id 取）
```

## 4. 验收后的收尾

- 六项全勾 → `openspec archive wire-content-fleet -y`（fd-wire 根）。
- 台账：`MCP-REGISTRY.md` 的 wire-build-mcp 行把工具数 15→17（若还没人改）；`HANDOFF.md` 补本批次一段。
- paas 侧知会：7 天凭据观测结果、（若动了 Logto 租户 TTL）告知 rotation 口径变化。

## 5. 10-24 交接执行（两段，最终 27/28）

进度 22/28 → **24/28**（5.2/5.3 已勾）。执行详情与证据：`reports/2026-10-24-handoff-execution.md`。要点：

- **新阻断缺陷（paas）**：pack 安装闸 `credentialConnected` 无 wire-customer/lex 分支 → 无 registry 凭据的真实客户装 pack 必被拒。这是 5.1/7.4 外部半边与 4.2 完整闭环的共同阻断，归 paas 会话修（一行分支）。
- **新开户缺口（wire-api）**：`resolve_cell_endpoint` 约定派生把 customer_id（email 型）当 cell 路径段 → email 客户引擎调用必 403；新付费客户需要 customer→cell 端点注册（AI_CELL_ENDPOINT_MAP 或 cell registry 查询）。
- **计量断链（wire-api）**：metering_records 空表，wire-ops 不发 usage 事件——5.1 计量断言无面可验，需口径裁决。
- **orchestration 复位复发**：13:53 报数实证第二次复位，窗口与 13:42–13:55 凭据升级/平台重启重合（复位者仍未定位）。收尾时双舰队均 supervisor。**观察期内每次巡线前照旧查**。
- **凭据续期 7 天观测**：aloadtree 两行正常；tester2 新行自动续 ✓；运营号行 14:47 invalid_grant（旧行铸于新镜像前、首轮续期晚于租户 RT TTL）→ **已重铸**，明早 09:00 巡线是重铸行首次真考验（若再失败：headless 重铸配方在执行报告 §四）。
- **headless 铸币能力已沉淀**（临时脚本+配方，见执行报告）：Logto experience API（PUT /api/experience 起、首方 consent 在 GET /consent 页自动过）+ 平台 line 凭据全链。前置：Logto Grant 行（本会话已为 platform-test/wire-tester2 补，14 天期）。
- **对话轨 LLM 上游今日 14:5x–15:4x 悬死**（deepseek-v4.1-flash 池；`deepseek-v4-1-flash-261017` 快照池当时可用）——已自愈。若复发且影响观察期，切 TOKEN_GATEWAY_MODEL 是用户裁决项。
- 到点物化复核：**CST 次日 04:20 后**查调度 id=2 的 last_status 与 materialized_artifacts。

### 追加（用户放行后晚间收口）：**27/28**，仅剩 1.6 观察窗

- **5.1/7.4 全勾**：tester2 专户 cell（wiretester2）全链打通 → PMI 数据集落客户域 → 内容岗落 **artifact 38**（客户域/404/source_ref 三元组全验）→ 7.4 三产物（36/38/9）同构断言全过。
- **4.2 全勾**：tester2 对话轨 conv 15 全链（读 21 行真数据→结论→批准→草稿落库，conversation_id 即来源指针）。
- **引擎开户口径已上产**：`AI_CELL_ENDPOINT_MAP`（fd-wire bcf57e3）。新付费客户开户五步配方在执行报告 §六-1。
- **orchestration 复位者**：静态排查到头（全护栏+零 PATCH 日志），治本=fleet 行更新审计 → 已入 paas 需求文档 P1。
- **paas 需求文档（用户处理）**：`docs/paas-wire-line-install-gate-and-fleet-audit-requirements.md` 四件（P0 安装闸分支/P1 fleet 审计/P1 凭据首连交付/P2 consent 页）。**P0 上产后撤 tester2 占位 registry 凭据行**。
- 待办移交：1.6 收口（含明早 09:00 重铸行首考）、调度 id=2 到点物化复核、metering 口径裁决。
