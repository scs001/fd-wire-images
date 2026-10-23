## Why

2026-10-11 生产事故实录：手动排空与自动 tick 并发摄取同一批审计事件时，`_ingest_record` / `_ingest_anomaly` 的 SELECT-then-INSERT 在事务间隙撞唯一约束（`uq_metering_request_server` / `uq_anomaly_request_reason`），UniqueViolation 使整批摄取回滚——轮询通道曾因此连续 24h 82 次失败、真实客户调用入账延迟远超 spec 承诺的 10 分钟。第十一轮已记录为候选小 change 待认领，本轮清账。

## What Changes

- 摄取服务两条写路径（计量记录、异常留痕）的落库语句改为 `INSERT ... ON CONFLICT DO NOTHING`（`index_elements` 对应各自唯一约束；PostgreSQL 与 SQLite 双方言同 API，按 `db.get_bind().dialect.name` 分发）。
- 以语句级 rowcount 判定插入/跳过：插入成功计 `accepted`/`anomalies`，冲突吸收计 `duplicates`/`anomaly_duplicates`——对外计数语义与既有 SELECT 预检路径完全一致，正常路径零漂移。
- 既有 SELECT 预检保留为快路径（常见重复重放不走 INSERT 往返）；竞态窗口收窄为「预检未见 + 并发已提交」时由数据库约束原子吸收，不再炸批。
- `request_id`/`server_name` 缺失的异常（无仲裁键）维持原 `db.add` 路径不变（NULL 在双方言唯一索引中互不相等，语义本就不去重）。

## Impact

- 代码：`api/fd_wire_api/metering/ingest.py`（两条路径 + 方言分发 helper）。
- 测试：`api/tests/test_metering_ingest.py` 新增竞态吸收用例（同语句二次执行不炸、rowcount=0 计重复）与双方言编译断言；既有幂等用例全量回归。
- 不涉及迁移、不涉及 API 契约、不涉及 web。
