# Proposal: wire-cell-storage-quota — cell 存储配额（pg_database_size 商业杠杆）

## Why

ROADMAP 方向 A 红线②：「50 万行上限从『保护 SQLite』转为商业配额杠杆
（`pg_database_size` 计量）并重估数字」。v1/v2 之后 cell 数据根已在 PG 上，
但**没有任何存储级保护**——行数上限（`CUSTOMER_DATASET_MAX_ROWS`，默认
50 万行）是 SQLite 时代的行数闸，宽度不受控（一列宽文本就能撑爆存储），
PG 根下成本敞口真实存在。本 change 落地**机制**：可选存储配额，超限整批
拒绝、诚实报错；数字 env 可配（商业定价与档位数重估留给产品决策，本文
不预设）。

## What Changes

1. **引擎可选存储配额**（fd-daas-mcp internal-main）：
   - env `FD_CELL_STORAGE_QUOTA_MB`（未设=零行为变化，两后端一致不设防）；
   - 仅 PG 数据根生效（sqlite 路径零触达——其保护仍是行数闸，行为字节
     不变）；检查=事务头一次 `pg_database_size(current_database())` 对照
     配额；
   - 生效面=全部增量写入路径：ingest_commit（全量快照）、增量 merge、
     write_batch、derive / join_indicators / link / derive_refresh（子集
     物化）——超限即 `CustomerDatasetError`（文案含「超过上限」「整批拒绝/
     整事务回滚」，api 侧 `_is_quota_error` 的「上限」匹配自然接住）；
   - overview 增可选字段：配额设置时回 `storage_quota_bytes` +
     `storage_used_bytes`（未设不回， additive 零破坏）。
   - **与行数闸叠加不替代**：行数上限原样保留（红线②的「重估」是替换
     决策，须用户拍板数字后才动，本文只加杠杆不加定价）。
2. **cell-manager 配额下发**：`CELL_PG_QUOTA_MB`（可选正整数）→ provision
   时注入 cell env `FD_CELL_STORAGE_QUOTA_MB`；幂等重入照注入；变更配额
   需重建容器（env 语义），docs 写明。
3. **配方文档**：docs/cell-postgres-storage.md 增「存储配额」节——env、
   计量口径（pg_database_size 全库含索引/WAL 语义说明）、变更流程（重建
   容器）、与行数闸的关系、定价重估待产品决策。
4. **试点**：pgauto001 重建注入配额——低于现用量值验证「整批拒绝+文案」，
   再升到生产参考值验证常规操作无感+overview 字段；drillcust001 滚新镜像
   不注配额（未设=不变，兼作无回归证据）。

## Impact

- fd-daas-mcp（internal-main）：customer_dataset_tools 增配额助手+七路径
  接入+overview 两字段；测试矩阵双后端（PG 腿含配额用例）。
- fd-wire：cell-manager config+provision 注入、docs、spec。
- 生产：cell 镜像一版（law-ai-official 快照→GHA→relay）；pgauto001 重建
  注配额、drillcust001 重建不注；api/web 零改动不滚。
