# 舰队实跑首日记录（wire-content-fleet 1.6 启动 + 4.2 前置闭环实证）

> 2026-10-24 04:00–08:50（北京时间）。运营号 platform-test@findmail.com cell（3fdc7b8e8ac03b68）。
> 本日结论：**巡线→派发→内容岗落产物→source_ref 三元组落库全链在生产跑通**；1.6 的一周观察期从今日起算。

## 一、已交付

| 事项 | 结果 |
|---|---|
| 「柏讯 · 产物构建」pack 发布 | `SWPvacoFTftfVngMMFotwQ` v1（此前 registry tool_list 快照停在 15 工具，发布门 unresolved；手动 PATCH 刷到 17 后过门） |
| 22 套件全量重发 | 全部携带 `wire-build-mcp` 声明（allowTools 最小集 5 工具）；`wire-equity-research` 的市场作者是 platform-test@findmail.com，须按市场作者精确匹配发版 |
| 运营号 cell 装包 | 技能 installed / wire-build-mcp installed（首次 awaiting-local-binding，registry-bridge 快照暖后重装即好）/ 双 agent installed |
| 舰队实例化 | `fleet_1792786329045_jchvsd` 三岗：巡线（cron 0 9 * * *）+ 内容（被动）+ 源（**vacant**，见缺陷①） |
| 巡线真信号回路 | wire-ops 实读需求单（DE-20261009-01 PMI 滞留 14 天）→ supervisor 派发边放行 → 内容岗/源岗真收单 |
| **内容岗产物落地** | artifact_id=36「园区批次-打药-采收 三表聚合看板」草稿 v1/v2，4 面板，列名与数值全来自真实数据集；**source_ref 三元组完整**：`fleet=fleet_1792786329045_jchvsd;post=m_1792786329045_kkmy1f;title=内容岗` |
| api+web 滚版 | sha-5b22762 → **sha-8b6a68b**（快照仓 scs001/fd-wire-images，源真 9db85fa）；PG 迁移 0040（source_ref 列）已在生产补跑并 stamp alembic_version |

## 二、实跑撞出的五个生产缺陷（当日全修）

1. **registry tool_list 快照滞后**（15 vs 线上 17）：发布门 fail-closed 报 unresolved。修法=抓线上 tools/list 后 `PATCH /api/servers/wire-build-mcp` 直写快照；并发会话的 add-self-reported-tool-surface（自报通道）上线后此类手工刷新退役。
2. **fleet MCP 不在生产 mcp.json**：`PACK_BASELINE_MCP=fleet` env 一直要求该条目，但 platform-mcp-config 缺失 → 所有 cell 的岗位报数/委派/拍板工具全缺位（巡线曾用 bash 读 fleets.db 裸凑）。修法=fd-infra-deploy `all-services/prod/platform.yaml` 补 `fleet` 条目（commit cb3cc3e），GitOps 收敛。**教训：PACK_BASELINE_MCP 与 ConfigMap 无一致性校验，纯靠运行时 warn**。
3. **wire-content-fleet 的 api 代码未上产**：source_ref 与 dashboard/doc 两步创建在生产 500（先 500 于缺列，后 401 于凭据，最后 500 于缺列）。修法=快照出片滚版 + 生产 PG 补跑 0040。
4. **`orchestration` 会被复位为 null**（supervisor 派发边消失，巡线一度连续被拒）：复位者未定位（疑 fleet reconcile 或 pack 相关路径重写 fleet 行），已挂账观察；复位后 `PATCH /api/fleets/:id {"orchestration":{"mode":"supervisor"}}` 一键恢复。
5. **凭据热交换不进 dsh 子进程**：line-credential 重铸后 mcp.patch.yml 已重写（00:13），但 dsh 子进程连读三轮旧 token（401），platform 重启（重 spawn）后 00:31Z 自愈。**无人值守舰队的结构性约束**：wire-customer 是短命 ID token（约 1h），静默续期依赖浏览器 Logto 会话——日节奏的巡线每天 09:00 都会撞过期。
   → **当日已闭环（paas 侧）**：需求文档 `fd-wire/docs/paas-line-credential-renewal-requirements.md` → paas change `add-line-credential-renewal` 全量交付上产（paas 2b8cc9a9 → 镜像 sha-f8f1505，迁移 v102 实证；P1 refresh token 服务端续期 + rotation 单飞 + P2 diff-restart；P3 永久搁置）。wire 侧零改动兑现。
   → **凭据行升级**（2026-10-24 13:42–13:46 CST，7 天观测起点）：admin（aloadtree）wire+lex 两行、fleet 属主 platform-test 的 wire-customer 行全部重连升级为带 refresh_token 新行（fleet 行 rt=1 实证，exp 起 1h）。**7 天观测窗口 10-24 → 10-31，与 1.6 观察期重合**。
   → **遗留一处 P2 缝隙（给 paas 的复核项）**：fleet cell（3fdc7b8e）在 13:44 cell 启动（当时行已过期）→ 13:46 铸新行（rt=1 落本 cell app.db）之后，本 cell 的 profile 重写仍持续判「no live credential for platform-test」（分组 warn），重装 pack 也只重写 skills patch 不动 mcp.patch；直到 13:55 platform 重启，boot 组合才带上双 wire 服务（token 注入 200 实证）。对照：admin 的 cell（1da519）在重连后**未重启**即正确重组（wire-ops 进 6→7 servers）。差异疑似 cell 进程内在铸币前缓存了「无有效凭据」态且 reapplyProfile 未击穿。重启后运行时已实证正常（tool_search 找到 10 个含 wire 的工具；库内 token 直打 wire-ops initialize 200）。该缝隙只在「cell 启动于行过期窗口内 + 进程内铸币刷新」的时序出现，日节奏 09:00 巡线 + 服务端续期（10min 巡检）的正常路径不受影响，但建议 paas 复核 reapplyProfile 的失效路径。
   → 观测注记：巡线回合的 tool_search 存在检索词假阴性（同一回合内先「No effective tool matches」后同批工具被找到），模型会据此误判「未接入」——后续判读 transcript 时先找有没有找到过工具，再下平台结论。

## 三、观测与调参记录

- 巡线回合时长：裸跑 10+ 分钟超时（模型做端点探测）→ promptTemplate 加「禁 HTTP 探测、只用已挂载工具、5 分钟内收尾」后 1.5–4 分钟稳定。
- 报数防洪 3 条/720 分钟：连续人工触发测试会打满窗口（12h），日节奏正常跑不受影响。
- 巡线 KPI 槽位 signals/dispatched 真实起算；内容岗 artifacts 槽位已记 +1。
- 决策通道（escalate）实证：巡线对超权限事项（凭据过期、源岗缺位）自动升级 owner 决策队列，符合设计 D4/D7。
- 源岗 vacant：spider-heal A2A 条目在萬星运营号 private pack，platform-test cell 目录解析不到（tasks 3.5 的 V1 验证项首答=不通过）。巡线会照实把源岗相关的信号升级而不是假装派发。

## 四、tasks.md 状态影响

- 1.6：一周实跑**已启动**（今日为 D1），产物落库与连续报数两条件已各有一次实证；观察期跑满（≥10-31）且空信号比例 <80% 才可勾。
- 4.2：抽查套件端到端「读数据→出结论→沉淀为看板草稿+来源指针」的**产物落库半边已由内容岗真跑实证**（36 号看板即来自真实数据集聚合）；完整走一遍宏观套件技能稿仍待做（用 conversation 轨跑 macro-analyst，非必须走舰队）。
- 5.1/5.2/6.x/7.4：未动。

## 五、复现实操（换环境照抄）

```bash
# 1. 发布 pack（作者须匹配市场既有作者；限流 10/小时/作者）
export FACET_INTERNAL_TOKEN=$(kubectl --context cheap -n fd-prod get secret facet-secrets -o jsonpath='{.data.FACET_INTERNAL_TOKEN}' | base64 -d)
python3 ~/.agents/skills/facet-pack-builder/scripts/publish_pack.py docs/wire-presets/pack-manifests/wire-artifact-build.json --author aloadtree@gmail.com

# 2. 铸平台会话（session secret 在 pod 内；probe-cluster-platform-live.mjs 同款）
SECRET=$(kubectl --context cheap -n fd-prod exec deploy/platform -- cat /data/auth/session-secret)
COOKIE=$(node /tmp/mint-cookie.mjs "$SECRET" "platform-test@findmail.com" "admin,wire-customers,creators")

# 3. 装→建队→启动→手动触发
POST /api/packs/SWPvacoFTftfVngMMFotwQ/subscribe
POST /api/mypacks/install {packId, version}
POST /api/fleets/from-template {packId}
POST /api/fleets/:id/start
PATCH /api/fleets/:id {"orchestration":{"mode":"supervisor"}}   # 派发边
POST /api/cron/:taskId/run                                       # 巡线 taskId 从 cockpit 取

# 4. 凭据（短命，浏览器静默铸）：platform-test 登录 → /api/lines/wire-customer/mint/start 直接 GET 即回环
# 5. 产物核验：PG artifact_versions.source_ref 三元组
```
