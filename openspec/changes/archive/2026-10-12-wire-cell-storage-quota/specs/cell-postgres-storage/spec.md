# cell-postgres-storage Specification（delta）

## ADDED Requirements

### Requirement: 可选存储配额

引擎数据根为 PostgreSQL 时MUST支持可选存储配额：env
`FD_CELL_STORAGE_QUOTA_MB` 设为正整数即启用，未设或非正则完全不设防
（两后端行为与现状一致）。配额检查MUST在全部增量写入路径的事务头执行
（全量快照提交、增量合并、批量写、派生、指标连接、link 物化、刷新重物
化）——已超限即整批拒绝/整事务回滚，错误文案MUST含用量、上限与「超过
上限」字样（api 侧配额语义识别可直接命中）。配额与行数上限MUST叠加生效
而非替代。sqlite 数据根MUST不受该 env 影响（行数闸仍其唯一保护）。

#### Scenario: 超限整批拒绝
- **WHEN** PG 根 cell 设置了配额且 `pg_database_size` 已超限，任一增量
  写入路径被调用
- **THEN** 整批拒绝/整事务回滚，错误含「超过上限」与用量数值，旧数据
  完整保留

#### Scenario: 未设零变化
- **WHEN** 未设置该 env（或值非法回落为未设）
- **THEN** 两种后端行为与现状完全一致（sqlite 根下设置该 env 也不产生
  任何新行为）

### Requirement: 用量可观测

配额启用时 overview MUST回带 `storage_quota_bytes` 与
`storage_used_bytes`（`pg_database_size` 口径）；未启用时MUST不回带这
两字段（响应形状与现状一致）。

#### Scenario: 配额观测
- **WHEN** 对配额启用中的 PG 根数据集调用 overview
- **THEN** 返回含配额与当前用量字节数，且 used 与库真实尺寸一致

### Requirement: cell-manager 配额下发

cell-manager MUST支持 `CELL_PG_QUOTA_MB`（可选正整数，非法值
ConfigError）：provision 时注入 cell env `FD_CELL_STORAGE_QUOTA_MB`，
postgres 画像与 sqlite 画像的注入行为一致定义（sqlite 下注入无害，引擎
侧不生效）。未配置时不注入该 env。

#### Scenario: 下发
- **WHEN** 以 CELL_PG_QUOTA_MB=512 执行 provision
- **THEN** 容器 env 含 FD_CELL_STORAGE_QUOTA_MB=512；未配置的 provision
  不含该 env
