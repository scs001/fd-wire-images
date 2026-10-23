# Tasks: wire-cell-storage-quota

## 1. 引擎（fd-daas-mcp worktree，origin/internal-main 基线 3198699）

- [x] 1.1 配额助手：env 解析（正整数启用，非法回落未设）+ `_enforce_storage_quota(cur)`
  （仅 `db_dialect.is_postgres()`；`pg_database_size(current_database())` 超限抛
  `CustomerDatasetError`，文案含用量 MB、上限 MB、「超过上限」「整批拒绝」）
- [x] 1.2 七路径接入（各事务头）：ingest_commit / 增量 merge / write_batch /
  derive / join_indicators / link / derive_refresh
- [x] 1.3 overview 增可选字段 storage_quota_bytes/storage_used_bytes（未设
  不回；PG 根+配额启用才回）
- [x] 1.4 测试矩阵：PG 腿配额用例（超限整批拒+文案+旧数据在；未设零变化；
  overview 两字段）+ sqlite 腿回归零变化（env 设了也不变）+ 全套双后端绿

## 2. cell-manager + 文档（fd-wire）

- [x] 2.1 config：CELL_PG_QUOTA_MB 可选正整数（非法 ConfigError）；
  provision 注入 env FD_CELL_STORAGE_QUOTA_MB；测试（注入/不注入/非法值）
- [x] 2.2 docs/cell-postgres-storage.md 增「存储配额」节（env/口径含索引/
  变更须重建容器/与行数闸叠加关系/定价重估=产品决策待办）

## 3. 发布

- [x] 3.1 引擎 push origin internal-main；fd-wire 快照推 law-ai-official；
  GHA cell 镜像；relay 至 ccr

## 4. 试点（生产）

- [x] 4.1 pgauto001 重建注入小配额（低于现用量）→ 增量写入整批拒绝+文案
  验证 → 再重建注 512MB 生产参考值 → 常规操作无感 + overview 字段实证
- [x] 4.2 drillcust001 滚同镜像不注配额 → healthy + 冒烟（未设=不变证据）
- [x] 4.3 （可选）api 侧 `_is_quota_error` 对引擎新文案的命中抽查——若
  不命中如实记录不改 api（cell 侧配额语义现状本就不经 api 转译）

## 5. 收口

- [x] 5.1 validate --strict 过；archive；ROADMAP 红线②标注机制已备待定价
- [x] 5.2 记忆更新
