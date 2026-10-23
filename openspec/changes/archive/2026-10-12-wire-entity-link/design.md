# Design: wire-entity-link

## Context

- 引擎（fd-daas-mcp internal-main@00699b4，工作树 /tmp/daas-entlink——本地
  DAAS 主检出有他人未提交改动，**引擎工作一律在 worktree**）：
  derive=单父查询物化（`_materialize_child_tx` 记血缘 parent/kind/spec），
  join_indicators=指标连接，refresh 按 kind 分发重放；行上限 `_max_rows`
  整批拒绝；列真相 `meta.columns_json`。
- api：entities 路由已有关系解析（from 实体数据集+列 / to 实体数据集+主键）；
  derive 登记先例=`_register_derived_child`（source_kind="derived"，
  source_ref=父 key）+ `_sync_columns_and_size` 回填；档位闸
  `require_cell_tier`（403 PLAN_TIER_REQUIRED）。
- 出片：fd-wire-images 快照仓 checkout fd-daas-mcp@internal-main 作 cell
  镜像构建上下文——**引擎 push internal-main 后，fd-wire 快照重建即带上**。

## Decisions

### D1. 引擎 link 工具（LEFT JOIN、撞名前缀、整批拒绝）

```python
def link(self, child_key, left_key, left_column, right_key, right_column,
         actor="", channel="console", _refresh=False) -> dict
```
- 校验：两侧 key 合法且互不相同、均存在；left_column ∈ 左集列、
  right_column ∈ 右集列（指名报错）；child 不存在（重物化走 derive_refresh）。
- 列计划：左列全保留（名/类型原样）+ 右列撞名 → `right_<原名>`（仍撞
  [极小概率] 则再递增 `_2`）、未撞名保留原名；子 pk=左 pk。
- 执行：单事务 `CREATE staging AS SELECT l.*, r.<c1> AS <out1>, … FROM
  <左 effective> l LEFT JOIN <右 effective> r ON l.<lc> IS r.<rc>`——
  **IS 连接**（SQLite `IS` 对 NULL 相等也成立；业务键一般非空，但语义一致）
  复用 `_materialize_child_tx` 的落表/血缘/上限路径（kind="link"，
  spec={left_key,left_column,right_key,right_column}）。
- refresh 分发：derive_refresh 读到 kind=="link" → 以 spec 重调 link
  （_refresh=True 跳过 child 存在检查，换表原子替换）。
- join 只走 effective 表（修正层生效——与 derive/preview 同口径）。

### D2. api 端点（entities 路由）

- `POST /api/v1/entities/{entity_id}/relationships/{rel_id}/materialize`
  body `{name?: str}`（≤200，缺省「{左实体名}×{右实体名} 连接」）。
- 解析：关系存在且以 entity 为端点（404）；取 from 实体（=左）与 to 实体
  （=右）的 dataset 行（datasets 库会话）；**两端 storage_tier==cell** 否则
  `plan_tier_error` 403；from_column/to_column 仍在各自列快照（漂移 422
  如实指名——声明时校验过，此处防漂移窗口）。
- 执行：`service.allocate_key`（连接子集 key）→ 引擎 `TOOL_LINK`
  {child_key, left_key=左集 key, left_column, right_key=右集 key,
  right_column=to_column, actor, channel="console"} → `_register_derived_
  child`（source_ref=左集 key）→ `_sync_columns_and_size`（引擎 overview
  回填，撞名前缀列自然入 columns_json）。
- 错误矩阵：引擎 CustomerDatasetError 域 → 4xx 如实（工具不存在=旧 cell →
  502 detail 带引擎原文）；EngineUnavailable → 502；零半写（引擎成功才登记，
  登记失败则引擎侧子集孤儿由 delete 端点既有路径可清，响应如实提示）。
- 配额：子集登记前走 `ensure_new_dataset_room`（免费档在 tier 闸已拦，
  付费档配额照常）。

### D3. web 动作

- 关系行（EntitiesPage）加「物化连接」按钮：仅当两端实体数据集 tier 均
  cell 显示（数据集摘要已带 storage_tier）；点击 → POST → 成功 toast 带
  新数据集名 + 「查看」跳 `#/console/datasets`；错误如实逐条。
- i18n zh/en；教程「实体」章节补物化小节。

### D4. 发布与滚动

1. 引擎 commit → push public/internal-main（FDT org git 通道，Actions 无关）。
2. fd-wire 快照仓推送（api/web/cell 三镜像重建；cell 构建上下文=新 internal-main）。
3. api+web manifest 滚新快照 sha；**drill cell 用 env-file 法原地重建**滚新
  cell 镜像（生产其余 cell 未滚——物化端点对它们如实报「工具不存在」，
   属预期窗口）。
4. MCP 层实弹冒烟（drill cell）：link 成功/撞名前缀/未命中 NULL/重物化/
   超限拒绝；api 层冒烟需 cell 档 console 客户（有则跑，无则以 MCP 层为准）。

## Risks / Trade-offs

- 双亲血缘 v1 只挂左父边（管线图/血缘端点见左父；右父在 link spec 与
  实体页关系里可见）——诚实降级，全双亲边另立。
- 大表 join 的引擎内内存：CREATE AS SELECT 全 SQL 侧执行不拉 Python 行
  （与 derive 的行拉取路径不同），受 `_max_rows` 事后校验——超限则子表
  已建再回滚（事务内 DROP 回退，保持整批拒绝语义）。
- 旧 cell 窗口期：物化在未滚 cell 上不可用，错误文案指向「联系滚动」。

## Migration Plan

零迁移（引擎侧用既有 customer_datasets 血缘字段；api 侧零新表）。
