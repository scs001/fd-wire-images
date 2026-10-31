# 柏讯迭代路线图 v2 —— 多 agents 架构下的分工与波次

> 定稿 2026-10-29。本文接替 `ROADMAP.md`（v1，2026-10-11）的方向锚职能；v1 保留作历史定调
> 与「已上产」现状台账，此后不再追加。**本文是唯一活路线图**，每次迭代收口更新波次勾选与现状行。
>
> 与 v1 的根本差异（两条用户拍板的定位修正）：
>
> 1. **搜索/发现不进柏讯控制台。** 数据发现与检索是 agent 的能力面：共享源（年鉴/法律/宏观面板…）
>    的搜索落在 business-mcp 的联邦搜索（`ai_search` / `graph_search` / 各域 `*_search`）；
>    客户自有 cell 数据的搜索对称落在 wire 入口的 MCP 工具。控制台不做 ⌘K 全局搜索。
> 2. **应用构建不进柏讯。** 寻数是多 agents 集群平台：应用 = 集群，在壹座组建（pack-as-cluster /
>    舰队模板）、在萬星上线运行、经谦面市场分发。柏讯是数据供给域——出数据、本体、产物与
>    数据面接口；**Workshop 式应用容器不在柏讯复刻。**

---

## 一、Palantir 对照是「平台整体」的对照，不是柏讯单独的对照

v1 把「类 Palantir」六层全部压在柏讯一个仓里盘点，切法错了。正确对照单位是寻数平台整体——
五线 + business-mcp 各承接 Foundry 的一部分器官：

| Foundry 器官 | 寻数承接方 | 现状（2026-10-29 实测） | 缺口与归属 |
|---|---|---|---|
| Data Connection 连接器 | 柏讯 | postgres/mysql/file 三类 + 上传 + 增量水位 | 广度窄 → 柏讯 Wave E |
| Ontology 对象/链接/属性 | 柏讯（本体）+ business-mcp/wire 入口（对象搜索面） | 实体 v0：名+数据集+主键；关系仅 n:1/1:1；AI 上下文注入；无属性元数据、无 m:n、无实例面 | 对象化 v2 → Wave C |
| Pipeline Builder 变换 | 柏讯 | 线性步骤链（derive/join/link）+ 版本槽位 + cron | 算子/DAG → Wave F（靠后） |
| Governance 治理 | 柏讯 | 溯源账本 + 证据包离线验签 + 双凭据计量（**强项，不输 Foundry**） | 敏感度传播 + ACL → Wave B |
| Contour/Quiver 交互分析 | 柏讯（owner 输出面） | 看板 = 快照渲染；query 仅为刷新契约 | **2026-10-29 拍板：不做实时渲染**，周期刷新维持 |
| Workshop 应用搭建 | **壹座** | pack-as-cluster + ClusterComposer + 舰队编排（fleet-template 契约） | 应用=集群路线已定，柏讯不做 |
| Actions 动作/写回 | 双层：壹座动作闸（平台动作）+ 柏讯动作闸（数据域动作） | 壹座五级风险三级确认；柏讯注册表 13 条全是平台自理动作 | 客户域动作 → Wave D |
| AIP / runtime | 萬星 | 上线运行/计费/观测；回合可靠性平台三缺口已修、2026-10-31 重普查验收过（Wave 0.1）；残余头号死因=上游模型网关 5xx（熔断/降级实弹零触发） | 平台件 → Wave 0；网关韧性挂账续（见 0.1） |
| Marketplace | 谦面 | 56 packs + 舰队 pack 铸造通道（fleet-packs） | — |
| 搜索/对象发现 | business-mcp（共享源）+ wire 入口（自有数据） | business-mcp 24 工具含 ai_search/graph_search；自有数据无搜索工具 | 对象/数据搜索工具 → Wave C |

一句话：**「类 Palantir」是平台目标。柏讯在平台里承担 Foundry 的「数据接入 + 本体 + 变换 + 治理」
与 owner 侧数据面；应用、运行时、市场、平台动作各有其主。** v1 的六层全景图分工仍成立，
唯独「⑥ 应用层」从柏讯 scope 划走（归壹座集群）。

## 二、需求落点判定表（「这个需求到底在哪完成」）

每个新需求先过这张表再立项：

| 需求类型 | 落点 | 理由 / 对外接口 |
|---|---|---|
| 客户自有数据的接入、同步、变换 | 柏讯 | 连接器 + cell 引擎 + 管线 |
| 客户自有数据的语义（实体/关系/属性/对象） | 柏讯 | 实体层；对外经 wire 入口 MCP 工具 |
| 共享数据源的搜索与读取 | **business-mcp** | 联邦 search/read（ai_search/graph_search/域工具）；柏讯不重复建 |
| 客户自有数据/对象的搜索 | 柏讯 wire 入口 | 与 business-mcp 搜索面对称的新工具（Wave C） |
| 四类产物（看板/表单/记录页/文档）的构建与分享 | 柏讯（owner 输出面） | wire-build-mcp + 控制台 |
| 把产物/数据/动作编排成持续运行的业务系统 | **壹座（集群）** | pack 内嵌 fleet 段；fleet-packs 铸造；萬星运行 |
| 业务动作的定义与审批（数据域） | 柏讯动作闸 | propose→confirm→execute→audit；效果落 overlay（ADR-0006） |
| 平台级动作（装包/建队/异步审批） | 壹座动作闸 | 五级风险三级确认 |
| 数据域计量/计费/配额 | 柏讯 | 双凭据归属口径不变 |
| 运行时计量/宿主计费 | 萬星 | caller-pays / host allowance / platform subsidy |
| 集群模板/技能/包的分发 | 谦面 | 市场是唯一分发更新通道 |

**反例记录（已误判过，不再犯）：**

- ~~控制台 ⌘K 全局搜索~~ → 搜索是 agent 能力面（business-mcp + wire 入口），控制台不做。
- ~~柏讯内做应用容器 / Workshop~~ → 应用 = 集群（壹座组建、萬星运行、谦面分发），柏讯只供数据与产物。

## 三、柏讯对外形状：三张 MCP 工具面 + 数据面 REST + 铸造线

柏讯的对外形状已从「B2B 控制台」变为「**三张 MCP 工具面 + 数据面 REST + pack/fleet 铸造线**」，
控制台只是 owner 的输出视图。现状盘点：

| 通道 | 内容 | 工具数 |
|---|---|---|
| wire 入口（registry） | 客户凭据、逐调用审计计量；数据集查询/聚合/行读 | — |
| wire-build-mcp | 看板/表单/记录页/文档创建更新发布、数据集列事实、按需建集（CSV）、数据治理 | 17 |
| wire-ops-mcp | 需求单三态、数据集只读查询/聚合/行读、市场目录/订阅/退订 | 13 |
| 数据面 REST | wdk_ key、per-cell、可选数据集允许清单 | — |
| business-mcp | **共享业务面**：core 读 + ai_search/graph_search/indicator_series_read、yearbook、law… | 24 |
| 铸造线 | pack manifest 内嵌 fleet 段 → 安装即出全暂停舰队；模板源 `fleet-packs/wire/`，packId 台账在 `fleet-packs/README.md` | — |

后续所有波次按这个形状设计：**新能力优先以 MCP 工具面落地（集群可消费），控制台其次。**

## 四、波次（顺序即执行序；每波独立可验收）

### Wave 0 平台前提（跨线，多数不在柏讯仓）

目标：无人值守集群达到「可交付外部客户」的最低可靠性。

- [x] 0.1 舰队回合可靠性（壹座/萬星）：按 `paas-fleet-turn-reliability-requirements.md` 三缺口收口
  ——失败可见性、未启动语义（0 秒 `runtime_not_ready`/同秒重复）、timeout 记录末步。
  **验收（2026-10-31 只读重普查，经 platform 控制台：8 支活跃舰队、437 条回合，按上产时刻分层，
  口径=非 ok 全收；平台侧修复四件 `fix-fleet-failure-visibility` 10-26 上产 /
  `fix-fleet-turn-veracity`、`fix-fleet-turn-attribution` 10-27 归档 / `add-cell-lifecycle` 10-28 归档）**：
  ✅ 状态与实际成功一致——连败 ≥3 岗位全部读 `failing`/`missing`（审读岗 cf=13 不再假正常；
  回执函请验的情报舰投稿岗已无 cf≥3 在列）；✅ `runtime_not_ready` 0 秒回合归零——55 条 0 秒记录与
  14 组同秒重复全部落在 10-24/25 历史，10-28 后窗口双双为 0（`skipped` 通道在位未触发=无「未执行」爆发）；
  ✅ timeout 可归因——窗口内 36 条 timeout 中 35 条带 `detail`。**新挂账（超出本波验收判据，不销账）**：
  上产后窗口失败率仍 57.3%（199 条：85 ok/114 bad），头号死因转为 `model_upstream` 68 条
  （上游模型网关 503/502 窗口，与 token 网关旧挂账同源；识律政心监测舰窗口内 0/6 全非 ok）——
  车道熔断+降级重试实弹零触发（0 条 `degradedTo`），即 `fix-fleet-model-drift` 5.2
  「等中毒复发验真机」所候场景正在发生；网关韧性转挂账，证据随本次分层数据递 paas 侧续推。
- [ ] 0.2 服务账号（柏讯仓，in-flight `add-fleet-service-accounts` 9/12）：按 tasks 走完含狗粮迁移。
  **2026-10-31 柏讯侧收口**：strict 绿 + 两枚口令全树零泄漏扫描 CLEAN（6.1 销）；
  剩 5.1（待壹座确认收号）、5.2（壹座切换后复核）、6.2 归档——球在壹座。
  **验收** = 现役舰队凭据全部脱离真人测试账号，审计能区分人与机器。
- [ ] 0.3 岗位身份注入（壹座 R1-a/R1-b）：`source_ref` 三元组真跑可落库。**验收** = 成员回合产物
  版本行带真实 fleet/post/member（见 `paas-wire-content-fleet-requirements.md` §三 P1）。
- [ ] 0.4 在途收尾：柏讯情报舰队（`wire-intel-sentiment-fleet-requirements.md`）、谦面 en 覆盖与
  platform admin 组修复的验证收口（10-29 已做，验后销项）。

### Wave A ~~数据域交互实时化~~ —— 已裁决不做（2026-10-29）

用户拍板：**不做实时渲染，维持快照 + 周期刷新**。理由：①查询型渲染对库压力大（数据库吃不消）；
②不存在真正的实时（上游本就是批量/水位同步）。周期刷新已由 dashboard-refresh +
refresh-scheduler + 级联刷新覆盖，本波全部撤销，不再排期。计量归属问题随之消失。

### Wave B 治理补洞（小而急）

目标：堵住企业安全审查一眼能看出的洞。

- [x] B1 **已实施（2026-10-29，双 change 代码全落、全 api 套件 1490 测全绿，strict 绿，
  已上产 2026-10-30 并归档）**：敏感度随派生闭包
  继承+闸时复检+向下传播撤回——①根仓 `wire-sensitivity-propagation`（数据集面主件：
  有效敏感度=祖先闭包取严、派生父集登记补全（join 右父现状丢失）+存量回填、设档闸
  闭包化、互锁向下传播、目录「继承」标记，`wire-asset-catalog` spec 归根仓故 change
  落根仓，沿 evidence-gate「change 落 spec 所在根」先例）；②fd-wire
  `wire-sensitivity-market-closure`（产物面双点闸闭包化，消费同一 helper 零第二实现，
  `artifact-marketplace` spec 归 fd-wire）。**验收** = 敏感父集→派生子集→上市场被拦、
  祖先变敏感连带撤回、兼容矩阵六行零回归——均已在测试与基准中实证（331 数据集
  闭包 5.1ms）。
- [x] B2 `wire-dataset-acl`（v1）：团队内数据集级授权；列级脱敏延后不排期。
  **已上产并归档（2026-10-31）**：迁移 0047 双库落位（`wire_datasets`/`wire_platform`
  版本=0047，19 张存量数据集回填 team）；单一判定 helper `datasets/acl.py`（owner 旁路/
  team 直通/restricted 查 grant，read|write）；拦截面=控制台列表过滤+详情/质量/导出、
  数据面 console 轨道全闸、实体对象三端点（graph 不拦）、写面需 edit；owner-only
  `GET/PUT /{id}/access` + append-only 审计；api 全量 1668 / web 1056 绿；
  真机冒烟（owner 真 token）受限切换/列表可见/回切/审计两条留痕全过。
  **v1 边界四条如实挂账**：wdk key 轨道 / MCP 工具面 / 产物快照 / 列级脱敏。

### Wave C 本体对象化 v2（柏讯仓）

目标：实体层从「元数据注释」升为「可浏览、可遍历、可被集群消费的对象世界」。
**状态：已实施并上产（2026-10-29 实施 / 2026-10-30 上产归档）——change `wire-entity-v2`（fd-wire 根，strict 绿），
C1-C4 合并一个 change（沿实体层 v0 单 capability 先例）。代码全落：模型列+迁移
0042（真链实证）、关系四基数（1:n 归一化存储/m:n 经中间集/`declared_view` 三处
同源）、对象浏览三端点（免费档 403、零行 found=false、多列主键 422 如实）、
wire-ops-mcp 三只读工具（注册探针 16 工具）、AI 注入 v2、web 声明表单+
EntityObjectBrowser+i18n+文档 v2。api 1505 全绿 / web 1024 全绿 / wire-ops 32 全绿。
待 rollout + 真库冒烟后归档。**

- [x] C1 `wire-entity-v2`（数据模型先行）：属性元数据（显示列/单位/格式）、关系补 `1:n`/`m:n`；
  存量 v0 声明零迁移成本（新键可选）。
- [x] C2 `wire-object-tools`（对外接口优先于 UI）：wire-ops-mcp 新增 `wire_object_search` /
  `wire_object_get` / `wire_object_relations`（verb_noun，与 business-mcp 的 search/read 形状对称）
  ——**集群搜客户自有对象走这里**；这就是「搜索放 business-mcp」修正的落点：共享源搜索在
  business-mcp，自有数据对象搜索在柏讯入口，控制台不做搜索。
- [x] C3 `wire-object-view`（owner 输出面）：对象详情页（属性+关联对象），读边界沿 record-view
  纪律（只执行声明查询）；免费档降级为声明+图谱（现状），对象浏览为付费档。
- [x] C4 AI 构建上下文注入改用对象口径（有界注入 ≤50/≤100 不变；空图谱零漂移不变）。

**验收**：声明两实体+关系后，集群经 MCP search→get→沿关系取关联对象全链通；AI 构建带对象语义。

### Wave D 动作域化（柏讯仓 + 壹座消费）

目标：动作闸从「平台自理动作」扩为「客户域动作类型」——分析落成运营的那一步。
**状态：已实施并上产（2026-10-29 实施 / 2026-10-30 上产归档）——change `wire-domain-actions`（fd-wire 根，strict 绿），
代码全落：迁移 0043（三表 + conversation_id 可空，真链实证）、类型 CRUD+校验矩阵、
效果执行器（引擎 write_batch strict anchors，通道 agent + domain-action:*）、
闸双源（resolve_action + on_proposed/on_rejected 钩子 + 域动作恒无会话）、
wire-ops-mcp 三工具（19 工具，tool_risk 契约履行）、web DomainActionsPanel
（构建器+台账+在途确认复用闸端点——镜像禁令）。api 1524 全绿（新增 19 测）/
web 1024 全绿 / wire-ops 32 全绿。待 rollout + 真库冒烟后归档（7.2 通知壹座
重烘焙快照是 pack 发布前置）。**

- [x] D1 `wire-domain-actions`：动作类型 = 客户在实体上声明（名称/风险档/表单字段/效果 =
  对目标数据集写 overlay 记录或状态列/审批档位）；进动作闸注册表（现 13 条，注释「十一条」顺手修正）；
  效果通道沿 ADR-0006（业务写并入修正层，不碰「永不写回客户源」红线）。
- [x] D2 `wire_action_*` MCP 工具（wire-ops-mcp）：`wire_action_list` / `wire_action_propose` /
  `wire_action_status`——集群岗位可提议域动作，owner 在柏讯控制台确认。
  **写操作永不进工具面直调（结构性保证，不变）。**
- [x] D3 壹座侧消费：舰队 escalate/拍板语义与柏讯异步审批对齐——出跨线需求文档到 paas（沿
  `docs/paas-*-requirements.md` 范式）。

**验收**：声明一个域动作→agent 提议→owner 确认→overlay 落记录→审计台账可查；未声明动作拒绝。

### Wave E 接入广度（柏讯仓）

- [x] E1 **已实施（2026-10-29）**——change `wire-rest-connector`（根仓，strict 绿；
  `wire-local-connector` spec 归根仓故 change 落根仓）：通用 REST/JSON 源适配器
  （声明式配置/分页/投影/可选增量水位/凭据只存连接器本地——云端零凭据不动）。
  实施实证：Go 适配器 14 单测 + `run --once` 两轮 e2e（首轮全量建基线、次轮只推
  水位之后的行、本地 token 不进云端报文）；api 侧 `datasets.source_config_json`
  （迁移 0044）与登记面凭据类头名拒收（**pydantic 422 会回显提交值，故校验移入
  路由层**）；web 登记表单零凭据输入 + 文档双语小节。
  **已上产 2026-10-30**：api 镜像 sha-ca7b45e 已滚；迁移 0044
  （`datasets.source_config_json`）双库落位；`/api/v1/connector/config`
  在生产 200/401 如实（未带连接器 key 即 401）。连接器二进制走既有下载端点。
- [ ] E2 SaaS 长尾按客户需求逐个立项（金蝶/用友/钉钉/飞书…），不预建。
- 流式/CDC：远期，不排期。

### Wave F 变换深度（最后）

**状态：F1+F2 已实施（2026-10-29）——change `wire-transform-operators`（根仓，
strict 绿；`wire-transform-pipelines` spec 归根仓故 change 落根仓）**：四个确定性算子
（filter/aggregate/union/project）+ SQL 步骤（只读三层护栏/占位绑定/字面量参数/禁危险
函数/超时行数闸/血缘可重放/试运行预览）一个 change 一次滚动（引擎五新工具，
DAAS 仓协调）。window 算子挂账 v1。

实施实证：引擎侧 46 测全绿（真 SQLite；含 filter→aggregate 链与 SQL 步骤的
真库冒烟对账）；api 1565 全绿（新增 22 测：六端点 + 管线八分支 + 旧引擎
降级 + 配额闸不旁路）；web 1035 全绿（四算子构建器 + SQL 编辑器试运行 +
双语文档）。三处偏离原草案已回写 spec/design：**物化聚合工具名带
`_derive` 后缀**（既有 `customer_dataset_aggregate` 是只读面板聚合，同名会
撞车）、**api 物化聚合端点为 `/aggregate/derive`**（既有 `/aggregate` 是
只读面）、**link 连接键两套键名都收**（实施期发现的既有口径裂缝：
控制台发 `left_keys`、实体端点发 `left_column`，只认一套会让另一边建的
步骤跑到执行才报错——已顺手收口并加回归用例）。
**已上产 2026-10-30**：引擎五新工具移植到 cell 镜像的实际构建线
`internal-main`（原只落 `main`，而 cell CI 检出 `internal-main` → 镜像内算子
计数为 0，已实测复现并修正）；`cell:sha-03916f2` 三支客户 cell
（wiretester2/pgauto001/drillcust001）全部升级 healthy。上产期真机暴露并修掉
两个 PG 腿缺陷（见 change `design.md` D7）：三处 `json.loads` 改 `_json_col`
（PG 驱动已反序列化 JSON 列，裸 loads 会炸）与 **SQL 步骤在 PG 数据根
fail-closed**（授权回调护栏是 SQLite 专属，PG 无等价机制，放开会越
「不泄露用户数据」红线；能力位按后端如实上报 `sql:false`）。真机冒烟：
PG cell 四算子全通、SQL 诚实拒绝；sqlite cell SQL 物化+试运行+危险语句拒绝
全通。两腿测试：sqlite 84 passed；PG（专用 `fd_engine_pgtest` 库）37 passed
/ 10 skipped（跳的全是 `@requires_sqlite` 执行类）。

- [x] F1 算子补齐：filter / aggregate / union / project（select-rename 并入 project；
  window 挂账 v1）——见 `wire-transform-operators`。
- [x] F1b `window` 算子（原挂账 v1）——**已上产并归档（2026-10-31）**，change
  `wire-window-operator`：引擎 `customer_dataset_window`（真 SQLite 47 测 + **真 PG 腿
  47 测**；并列以 `__row_hash` 定序、rank/dense_rank 保留并列）；api 步骤类型+数据面
  端点+血缘重放（16 测）；web 构建器 + 双语文档；cell:sha-48e2e93 三支 cell healthy
  （registry 实测 customer_dataset 32 工具、window 在列）；真库冒烟 smoke_with_pmi →
  row_number 1..5 / running sum 99.9→203.2 手算一致。**上产期修既有缺陷**：
  `models/__init__.py` 缺 CustomerDataset 四导出（508fa75 误删）致 cell 引擎组加载失败。
- [x] F2 SQL step 护栏：只读连接（引擎侧 query_only）/占位绑定收窄到显式数据集清单/
  字面量参数/禁危险函数/超时与行数闸/血缘可重放/试运行预览——同 change。
- [ ] F3 DAG/异步执行器：v2 挂账维持，不排期。
  **状态（2026-10-31）：设计已交——change `wire-dag-executor`（根仓，strict 绿）**：
  DAG 形状（`inputs`，线性=退化形状零迁移）、拓扑执行+分支并发、DB 租约队列异步执行器
  （短等待窗兼容同步调用方）、节点级状态/断点恢复/重试/超时/取消、血缘多父。三批施工
  （schema→executor→web）排在本轮 ACL/window 上产之后。

### 维持现状、不新增投入

- 供给引擎（方向 C）：需求单→自动出单→舰队→交付 scope 全链已在役；维护模式（conformance 门、
  health-loop、合规规则集扩充随需求走）。
- 个人信息工作台（方向 D）：薄工作台维持；C 端能力随 Wave A/E 间接受益。

## 五、裁决记录（2026-10-29 四项拍板全落）

| # | 决定 | 裁决 |
|---|---|---|
| D1 | 看板实时读取的计量归属 | **不做实时渲染**——维持快照+周期刷新（数据库吃不消，且上游本无真正实时）；Wave A 撤销，计量问题随之消失 |
| D2 | 客户域动作是否立项 | **立项**——change `wire-domain-actions`（Wave D）；壹座侧消费需求文档（`docs/paas-domain-action-requirements.md`）交用户处理 |
| D3 | 只读 SQL 子集是否放开 | **放开**——条件=不泄露用户数据；护栏见 Wave F2（cell 进程隔离=结构性无跨租户面） |
| D4 | 波次优先级 | **按默认顺序执行**；Wave A 撤销后实际序 = 0→B→C→D→E→F |

## 六、维护纪律

- 每波立项走 OpenSpec（propose → apply → archive）；本文只记「做什么/为什么/验收」，不承载实现细节。
- 跨线需求沿既有范式出需求文档（提出方 fd-wire → 对象仓，状态 待评审→已采纳→已落地），
  范式样例：`docs/paas-fleet-turn-reliability-requirements.md` 等。
- 每次迭代收口：勾选波次项 + 更新 §一/§三 现状行；**不新开第三份路线文档**。
- 批量爬取/生成会话收尾必须过 `fd-industry-data/scripts/conformance_gate.py`（AGENTS.md 硬门）。
