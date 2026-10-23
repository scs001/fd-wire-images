# Tasks: wire-artifact-marketplace

## 1. 数据层（0036，AiBase）

- [x] 1.1 `artifacts` 增列：`visibility`（默认 'private'）、`access_mode`
  （默认 'open'）、`published_at`、`published_version`、`allow_copy`
  （默认 false）、**`copied_from_artifact_id`**（Q7 溯源，nullable）
  ——布尔/字符串默认 PG 兼容写法
- [x] 1.2 新表三：`artifact_market_subscriptions`（customer_id × artifact_id
  唯一）、`artifact_share_grants`（定向名单唯一）、`artifact_access_requests`
  （pending|approved|rejected 唯一），挂 AiBase
- [x] 1.3 链头断言翻新（0035 → 0036，含 fd-wire 根单链测试）；
  双方言验证（SQLite 全量 + PG14:5433 upgrade/downgrade）

## 2. 市场服务（api/fd_wire_api/market/artifact_*.py，照 dataset 版同构）

- [x] 2.1 `artifact_service.set_visibility`：单写入口——仅 owner/editor；
  仅 published 可非 private 档；**绑定数据集敏感度硬闸（Q5：market 档
  422）**；幂等；append-only 留痕（status_history 追加 visibility 事件，
  8 字段 before/after）；撤回清 published_*
- [x] 2.2 `artifact_browse`：SQL 层 `visibility='market'` 过滤的列表/检索/
  详情（**Q8：面板结构摘要仅 {id,type,title}——无 config/snapshot/query**）；`resolve_renderable(artifact_id,
  customer_id)`（订阅/授权/自有三源判定，实时查表无缓存）
- [x] 2.3 订阅/退订/申请/审批（approve 幂等建订阅，同数据集市场）
- [x] 2.4 复制语义 `copy_artifact`：allow_copy 闸 + 订阅闸 → spec 深拷贝到
  caller draft；**快照剥离（Q4：递归移除全部 snapshot 键）**；面板绑定
  重映射（有访问权保留 / 无则标 unbound）；逐面板绑定报告；
  `copied_from_artifact_id` 留痕；复制后版本独立

## 3. API（market/routes.py 追加产物面）

- [x] 3.1 `PUT/GET /api/v1/artifacts/{id}/visibility`（四档 + access_mode
  + allow_copy + grants）
- [x] 3.2 `GET /api/v1/market/artifacts`（检索/筛选/分页）+
  `GET /api/v1/market/artifacts/{id}`（详情）
- [x] 3.3 `POST/DELETE .../subscribe`(+/unsubscribe) +
  访问申请/审批四端点
- [x] 3.4 `GET /api/v1/market/artifacts/{id}/render`（订阅者取版本 spec——
  **准入=订阅校验 + 敏感度双点闸（Q5）**；返回体带快照时间标注素材
  （Q3）；零引擎调用）
- [x] 3.5 `POST /api/v1/market/artifacts/{id}/copy`

## 3b. 快照时间与数据集互锁（Q9/Q13）

- [x] 3b.1 刷新路径写 spec `refreshed_at`（artifacts/refresh.py 产版本处）
- [x] 3b.2 **数据集敏感度互锁顺带修**：assets PATCH 改敏感度为「敏感」时，
  该数据集若非 private 档 → 自动撤回 private + 留痕 + 测试

## 4. 渲染路扩展（share/service.py）

- [x] 4.1 抽 `subscriber_spec_view(db, artifact, customer_id)`：准入=
  订阅关系实时判定 + 绑定数据集敏感度实时判定（Q5 渲染点）；
  **零改动既有链接路径**（回归钉死）

## 5. 控制台

- [x] 5.1 产物页可见性选择器（四档 + 开放模式 + allow_copy 开关）——
  复用 DatasetVisibilitySection 交互范式，挂 published 产物行
- [x] 5.2 市场页三页签（数据集 / 看板 / 报告，**默认数据集页签（Q14）**，
  页签带计数徽章）+ 产物卡片（订阅、复制到我的账号——条件可见）+ 详情面板
  （看板=结构摘要；报告=`data_scope`/`data_range` 元信息（Q11））
- [x] 5.3 复制结果报告呈现（bound/unbound 逐面板，unbound 带编辑器跳板）
- [x] 5.4 i18n 双语（Edit 工具插键 + 对称差 0 + tsc 零错）

## 6. 测试与验收

- [x] 6.1 市场闸矩阵：默认私有（SQL 层）/四档往返/撤回即失效/approval/
  allow_copy=false 403/directed 名单/**敏感度双点闸（Q5：发布 422 +
  渲染拒）**
- [x] 6.2 复制三态（全绑定/部分 unbound/全 unbound）+ 复制后独立性 +
  **快照剥离递归断言（Q4）+ 溯源指针（Q7）**
- [x] 6.3 渲染路：订阅者 200（含快照 + 时间标注素材）/未订阅 403/撤回后
  403/**详情结构摘要（Q8：无 config/snapshot/query）**/既有链接路径零回归
- [x] 6.4 前端：选择器/页签/复制按钮条件/报告渲染（vitest）
- [x] 6.5 全量回归：api + web + tsc + 双方言迁移

## 7. 文档与发布

- [x] 7.1 DocsView 数据市场章节增产物开放说明（只读看 vs 可复制）；
  ROADMAP 方向 B 勾注
- [x] 7.2 出片（law-ai-official 快照线）→ 生产迁移 0036（备份先行）→
  滚版 → 生产冒烟（发布看板到市场 → 另一客户订阅 → 渲染含时间标注 →
  复制（验快照已剥离 + 绑定报告））
- [x] 7.3 文案纪律落地核查（Q1：市场/渲染/发布弹层三处如实文案，
  禁止「仅结构不含数据」措辞）
- [x] 7.4 归档 + 记忆 + ROADMAP 结项勾注（Q15：三件必须项一组留痕）
