# Tasks — wire-free-tier-data-gate

## 1. 数据模型与迁移（api）

- [x] 1.1 datasets 库 alembic 迁移：数据集表加 `storage_tier`（`api`/`cell`，存量默认 `cell` 不回填语义）；新增 `dataset_columns`（dataset_id/name/dtype/ordinal）与 `dataset_rows`（dataset_id/row_index/data JSON）两表；迁移幂等可重跑，drift check 通过
- [x] 1.2 限额配置：`FD_WIRE_FREE_MAX_DATASETS`（默认 3）/`FD_WIRE_FREE_MAX_ROWS`（默认 10000）环境项落 config；单测覆盖默认值与覆盖值

## 2. api 层存储引擎（api）

- [x] 2.1 写入路径：按 `dataset_columns` 归一 dtype（数值/布尔/文本保守推断），行数据写 `dataset_rows`（JSON），行数闸超限拒绝并返回余量；单测含类型归一与超限
- [x] 2.2 查询路径：过滤（eq/in/gt/gte/lt/lte/contains/startswith）/排序/分页/count/distinct，SQLAlchemy JSON path 双方言（SQLite 与 PG 均有单测或方言冒烟）
- [x] 2.3 聚合路径：group by ≤3 列 × count/sum/avg/min/max；非数值列聚合拒绝错误清晰；单测覆盖空组与类型边界
- [x] 2.4 schema 预览：列/类型/行数统计端点复用 `dataset_columns` 聚合

## 3. 上传分流与档位闸（api）

- [x] 3.1 上传按档分流：tier=api 的上传件走 §2 存储（CSV 解析/类型推断复用或抽取既有上传代码为共享函数，不复制实现）；tier=cell 走既有 ingest 链路零变化；免费档上传全链路不经引擎（mock 引擎断言零调用）
- [x] 3.2 建集闸：免费档建集超 3 个拒绝（错误含余量）；连接器建集/derive/join/batchWrite 对无 cell 客户返回明确档位错误码（沿用或新增 403 语义）
- [x] 3.3 计量口径断言：api 层数据集查询/聚合不产生 metering_records 行数入账（D7）；限流沿用数据面既有 rpm

## 4. web 放行与限额 UI（web）

- [x] 4.1 DatasetsPage 免费档放行：整页升级墙退位；配额条内联（已用 X/3 集、每集 ≤10,000 行）；建集/行数触限时内联升档提示；付费档渲染零变化（快照测试）
- [x] 4.2 连接器区与派生入口免费档可见但标注付费：点击呈现升级说明；i18n ZH/EN 双语补齐
- [x] 4.3 UploadPanel 免费路径：上传成功后列表/预览走 api 层数据集（与 cell 层数据集同列表，来源徽标区分）

## 5. 验证

- [x] 5.1 免费路径 E2E（测试态）：注册免费客户 → 上传 CSV → schema 预览 → REST 查询/聚合正确 → 第 4 个数据集被拒 → 行数超限被拒
- [x] 5.2 付费回归：既有 cell 层上传/查询/派生/连接器测试全绿；免费档断言不触碰 EngineToolClient
- [x] 5.3 双方言冒烟：§2 两表在 SQLite（既有测试）与 PG（若有本地实例则真连，否则 SQLAlchemy 方言编译断言）下建表与查询可执行
