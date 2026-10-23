## ADDED Requirements

### Requirement: 摄取竞态安全

计量摄取服务（HTTP 推送与轮询归集共用）在并发摄取同一事件时 SHALL 以数据库唯一约束原子吸收冲突：冲突键已由并发事务提交的场合，本事务 SHALL 将该事件计为重复（`duplicates`/`anomaly_duplicates`）并继续处理批内其余事件，MUST NOT 因唯一约束违规使整批摄取失败或回滚。

#### Scenario: 并发同键摄取不炸批

- **WHEN** 两个摄取事务（如手动排空与自动 tick）先后处理同一 `request_id + server_name`（异常另含 `reason`）的事件，且后者的 SELECT 预检发生在前者提交之前
- **THEN** 后者的 INSERT 被唯一约束以 ON CONFLICT DO NOTHING 吸收，该事件计为重复，批内其余事件照常入账，事务不回滚

#### Scenario: 正常重复重放语义不变

- **WHEN** 同一事件在无并发的情况下被重放摄取（预检可见历史行）
- **THEN** 计数仍为重复且不产生第二行，行为与既有幂等口径一致

#### Scenario: 双方言一致

- **WHEN** 摄取服务运行在 PostgreSQL（生产）或 SQLite（测试）任一方言
- **THEN** 冲突吸收语义一致，均不抛出唯一约束违规
