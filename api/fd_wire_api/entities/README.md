# 实体层（entities）——模块约定

实体语义层：`entities` / `entity_relationships` 两表（本质是客户域元数据，不是数据行），
加上本体版本机制的两张表 `ontology_events` / `ontology_snapshots`（alembic 0026 / 0042 /
0049）。端点 `/api/v1/entities/*`（声明/图谱/对象浏览）与 `/api/v1/ontology/*`（版本面）。

## 硬约定（违反后症状滞后，务必先看这条）

**本体的唯一写入口是本包的 `service.py`。**

- 三条写路径（控制台 CRUD / 动作闸执行器 / 回滚执行器）全部经 `create_entity`、
  `update_entity`、`delete_entity`、`create_relationship`、`delete_relationship`；
- 每次写在同一事务内追加 `ontology_events`（before/after 全量），**事件写失败即本体写失败**；
- **绕过 service 的直改（裸 SQL / 直接 ORM 改行）不进事件流**：版本回放看不到它、
  完整性自检会报不一致。仅限事故运维，且事后应立即打一份手动发布版并记录。

新增一条写路径时的做法：给 service 函数传 `actor` / `via` / `source_ref` /
`source_instance_id`，不要另开写入实现（事件与校验都挂在这一层）。

## 文件职责

| 文件 | 职责 |
|---|---|
| `models.py` | ORM（实体/关系 + 事件/快照）；事件与快照与本体**同库** |
| `db.py` | 库归属与引擎（生产 PG `wire_platform`；本地 SQLite 默认） |
| `service.py` | **唯一写入口** + 校验 + 级联删除 + 图谱/提示块同源 |
| `versioning.py` | 规范 manifest / digest / 事件折叠 / diff / 回滚 plan 与执行 / 完整性自检 |
| `snapshots.py` | 快照创建（版本单调、同 digest 幂等）+ 日快照循环 + 日版保留清理 |
| `routes.py` | `/api/v1/entities/*` 与 `/api/v1/ontology/*` |

## 语义文档

版本语义、写面纪律、导出格式与四条恢复线见仓根
[`docs/ontology-versioning.md`](../../../docs/ontology-versioning.md)。

## 测试

- `api/tests/test_entities.py` / `test_entity_objects.py` / `test_entity_write_surface*.py`
  ——既有读面与闸通道（零漂移回归网）；
- `api/tests/test_ontology_versioning.py` ——事件流 / 快照 / diff / 导出 / 回滚 / 完整性；
- 迁移头断言散在各 `test_*migrations.py`：新增迁移后记得同步 head 常量。
