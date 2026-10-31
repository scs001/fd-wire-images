# tasks — xingyan-content-supply

> 硬前置：姊妹根 `xingyan-claim-layer` 组 2/3/6 收口（三 feed 含 claims 载荷与 manifest 指纹公网可达）。组1 勘验结论决定组3 是否需先补 entity-layer 的 MODIFIED delta。
>
> **apply 结论（2026-10-31）**：组1 勘验发现两件事，均已就地处置——① feed 顶层缺 `schema_version`，已回姊妹根补齐（其 spec 同步加「契约版本在场且一致」要求与场景）；② 目录承载不足以外接，结论是**新建独立 `xingyan` 切片**而非扩 entity-layer/catalog（理由见 design D-S1 修正段），因此**不需要 entity-layer MODIFIED delta**。

## 1. 接口核验与承载勘验（先行）

- [x] 1.1 feed 接口核验：三端点与 manifest curl 公网可达、载荷逐区含 claims 骨架且与姊妹根 spec 逐字段一致；若 feed 顶层无 `schema_version` 字段，回姊妹根接口议定补齐（不自行猜版本）；验证：核验清单逐项通过，留接口快照报告
  - **实测**：三端点顶层原为 `{generated_at, <zone>[]}`，**无 schema_version** → 回姊妹根 `fd-official-web/src/lib/xingyan.ts` 补 `XINGYAN_FEED_SCHEMA_VERSION='1'` 并写入三 feed；`check-discoverable.mjs` 加「必须为 1」的构建门；`xingyan-claim-layer` 的 machine-surface spec 加契约版本要求。本地构建实测三 feed 均 `schema_version=1`，门绿。
  - 载荷实测：`claims` 按语言分侧 `{en:[], zh:[]}`；仅 `law-bench-evaluation-loop` 有 3 条（zh 3 / en 3），其余条目 0——与姊妹根「条目 MAY 声明 claim」一致，不是漏抓。
- [x] 1.2 目录承载勘验：查 entity/目录注册路径对文本正文条目的承载（列=slug/分区/双语标题/摘要/日期/claim 计数/正文指针），输出「可直用/需扩 spec」结论；验证：结论附代码位点（文件:行号），需扩则先开 entity-layer MODIFIED delta 再进组3
  - **结论：不可直用，需新表**。位点：`assets/routes.py:386,418-422`（assets 强绑 `datasets.id`+`customer_id`）、`assets/models.py:61`（可见性四档 `private|link|directed|market`，刻意不含"公开"）、`catalog/models.py:54-92`（`IndicatorSnapshot` 无正文/claim 列）、`catalog/sync.py:41`（`_SYNC_FIELDS` 封闭元组）。最接近的非表格文本先例是 `IndicatorSnapshot` 的双语长描述。
  - **处置**：新建 `api/fd_wire_api/xingyan/` 独立切片（自有 Base + 三表），语义与 catalog 同构（只读镜像、唯一写入口=同步管道）；**不改 entity-layer/catalog 现有要求**，故不产生 MODIFIED delta。

## 2. 快照摄取链路

- [x] 2.1 快照表建：`xingyan_items`/`xingyan_claims`（键=slug 整体替换、`source_sha`/`fetched_at`/`schema_version`/`status(active|withdrawn)`）；迁移走一次性 Pod（在册先例）；验证：表结构对账姊妹根 feed schema 全字段无损
  - 迁移 `api/alembic/versions/20261031_0046_xingyan_content_supply.py`（`has_table` 早退=可重跑）；测试实测 upgrade→upgrade→downgrade 三态。五处链头硬断言已随 0046 推进。
- [x] 2.2 摄取作业：manifest 指纹短路 → 三区全量原子拉取 → upsert；schema 不匹配/拉取不全即拒摄留痕，上一版继续服务（D-S2/D-S3）；抓取走 mesh 内网域名+NO_PROXY（D-S4）；验证：同指纹二连跑零差异；构造 feed 不可达演练陈旧标记在场；构造半版本失败无缺席误标
  - `sync.py`：`fingerprint()` 只取三区 (items,sha)；`_collect()` 先 manifest 后三区，任一 zone 的 `schema_version` 不在兼容集即 `FeedSchemaMismatch`；zone 条目数与 manifest 声明不符即按半版本拒摄；`_apply()` 单事务，异常回滚。
  - **实测**：本地 dist 真链路 run1 `ok items=18 claims=3 zones={4,11,3}`，run2 `skipped`（零写入）；对公网旧构建（无 schema_version）实测 `schema_mismatch` 且旧快照条目数不减、`stale=True`。
  - 坑：会话 `autoflush=False` 使 commit 前计数为 0 → 计数前显式 `session.flush()`（已改，测试覆盖）。
- [x] 2.3 撤销语义：三区全成后对「表内有 feed 无」标 withdrawn（可逆），claims 同步撤销态；验证：演练撤稿→显式撤销态→复现回 active
  - 测试 `test_absent_slug_marked_withdrawn_not_deleted` / `test_withdrawn_slug_reappears_as_active` / `test_withdrawn_claim_is_explicit_state` 三条覆盖；claim 撤销**保留登记**（不物理删）。

## 3. 目录供给与可见性

- [x] 3.1 目录注册：按 1.2 结论注册 `xingyan_article`（业务名+数据集+主键），列表/检索面可过滤分区与日期；验证：目录发现场景（spec scenario）以真实查询复现，匿名访问走既有档位闸
  - 落地为独立只读目录面 `/api/v1/xingyan/items`（分区 `zone` + 关键词 `q` + 分页 + `status`）；摘要字段形状与数据条目同构。**与 spec 原场景的差异已写回 spec**（承载边界段）：不并入租户数据集目录，理由=平台级公开件 + assets 四档无"公开"档 + 绑 datasets.id 会伪造行。
- [x] 3.2 claim 解析端点：`slug/claim-id` → 骨架+锚小节内容+证据集；双语同指向；撤销态显式返回；验证：spec 三 scenario 逐条真数据复测
  - `/api/v1/xingyan/claims/{slug}/{claim_id}?locale=`；`extract_section()` 按发布门校验过的确切标题文本截取小节正文，找不到回 `section_found=false`（不给空串冒充）；证据 key 解析成 `{key,resolved,label,url}`，解不到标 `resolved=false`；撤销 claim 回 **200 + status=withdrawn**（非 404）。
  - **实测**：`law-bench-evaluation-loop/c0001` zh 得「结论先行」正文、en 得「Bottom line first」正文（1505 字符），证据 `src-1` 解析到真实 docx URL。

## 4. 工具面与上线

- [x] 4.1 MCP 工具三件（目录列举/条目检索/claim 取回）实现并显式声明；响应携带指纹+staleness；`PATCH /api/servers/<path>` 全量换 tool_list（在册唯一通道）；验证：registry pod 内 initialize+tools/list 在线探测，可见集与声明逐名一致
  - `wire-ops-mcp/wire_ops_mcp/tools_console.py`：`wire_content_browse` / `wire_content_search` / `wire_claim_resolve`（均为 `动词_名词`，只读）；`tool-surface.json` 19→**22** 并补 data_sources/notes。`wire-ops-mcp` 32 测试绿。
  - ⏳ **registry PATCH 与在线探测未做**（需出片后执行，见 4.2）。
- [ ] 4.2 镜像与部署：代码落 `internal-main` 出片；部署后重烘焙 registry 快照；验证：探面配方复跑绿（快照不重烘焙=工具面探不到）
  - 代码侧已就绪（api 1600+ 测试绿、mcp 32 测试绿）；出片/部署待放行。
- [ ] 4.3 节奏接入：每日 CronJob + 按需刷新走 refresh-scheduler；验证：手动触发一次全链（上游→表→目录→工具面）时延可测
  - **实现偏离**：未走 refresh-scheduler（其 `TARGET_TYPES` 只接受 dataset/artifact 且是**客户级可配置**刷新，平台级内容摄取不属其语义）。改为进程内每日循环（`runner.py`，照 alerts/catalog 先例）+ `POST /internal/xingyan/sync?force=` 手动补跑；对账脚本兼任「发现落后」通道（CronJob 停机不补跑教训的兜底）。

## 5. 对账与收口

- [x] 5.1 只读对账脚本：逐区比对 feed 快照 vs 目录 vs claim 索引（条目数+claim 集合+指纹），差异清单指名三处取值，零差异方可宣告；验证：构造一处漂移被指名、全绿报告含时点指纹
  - `api/scripts/xingyan_reconcile.py`（`--feed-dir` 本地产物 / 默认线上；`--json`）；四类差异：`missing_in_snapshot` / `extra_in_snapshot` / `claim_set_drift` / `claim_count_drift`，每条带三处取值。**实测**：本地库 vs 本地 dist → `✓ 三处零漂移`，报告含 `source_sha=3ad6a50b…` 与对账时点。
- [ ] 5.2 全量验收：spec 五 Requirement 的场景逐条真数据复现（含幂等/陈旧/拒摄/撤销/未声明面不扩张）；对账收口留痕归档；验证：七场景全过 + 姊妹根 feed 变更联动演练一次（改 zh 标题→全链到新工具面响应）
  - 本地已过：**24 条自动化用例**（幂等/陈旧/拒摄/半版本/撤销/双语 claim/锚点缺失/只读保护/目录过滤/迁移三态/**HTTP 路由层**）。**在线 tools 探测与公网全链待 4.2 出片后执行。**
