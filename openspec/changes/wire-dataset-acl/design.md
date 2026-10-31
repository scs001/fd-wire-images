# wire-dataset-acl — Design

## D1 存储：模式列 + 授权表 + 审计表（数据集库）

- `datasets.acl_mode` varchar(16) NOT NULL DEFAULT `'team'`，值域 `team|restricted`
  （应用层校验，同 storage_tier 口径）；默认值即零迁移兼容面。
- `dataset_grants`：`dataset_id`(36, idx) + `logto_sub`(64) + `permission`(16,
  view|edit) + `created_by_sub`(64) + `created_at`；`UNIQUE(dataset_id, logto_sub)`。
  无 FK：成员表 `customer_members` 在平台库、本表在数据集库——跨库以 sub 弱引用，
  沿实体层 `entities.dataset_id` 同款纪律（服务层校验，不落 DB 约束）。
- `dataset_acl_events`：append-only（只 INSERT）：`dataset_id`, `actor_sub`,
  `action`(mode_change|grants_replace), `payload_json`（旧/新模式与名单快照）,
  `created_at`。不改写、不删除，与 `team_events` 的审计纪律同构（但同库，避免
  跨库事务）。

## D2 判定：单一 helper，一处真源

`datasets/acl.py::can_access(db, *, customer, actor_sub, role, dataset, need)`：

1. `actor_sub == customer.logto_sub` → 通过（owner 旁路，owner 不在成员表）；
2. `dataset.acl_mode == "team"` → 通过（存量语义）；
3. `restricted` → 查 `dataset_grants`：`need="read"` 要 view|edit 任一，
   `need="write"` 要 edit；无行 → 403。

控制台读面取 `require_console_actor`（已有，返回 `(customer, sub, role)`）；
数据面 console 轨道 `DataPlanePrincipal` 增 `member_sub: str | None`（由
`require_console_customer` 旁取 sub；`data-key` 轨道恒 None——D6 边界）。
**不许第二份判定实现**：所有拦截点调同一 helper。

## D3 拦截清单（v1，逐条可测）

| 面 | 端点 | 行为 |
|---|---|---|
| 控制台列表 | `GET /api/v1/datasets` | 过滤掉无 view 授权的 restricted 行（不 403 整页） |
| 控制台详情 | `GET /api/v1/datasets/{id}` 及列/行/质量/导出各面 | 无授权 403 带可读原因 |
| 数据面 console | `dataset_query` / `aggregate` / rows / export / filter/project/aggregate/union/sql_preview 等六算子读面 | 无授权 403 |
| 实体对象面 | `objects` / `objects/{key}` / `relations`（含 m:n 遍历） | 绑定数据集无授权 403 |
| 写面 | restricted 数据集上的 PUT/派生/物化/批量写 | 需 `permission=edit`（叠加全局角色门禁） |
| 结构面 | `GET /api/v1/entities/graph`、实体元数据 CRUD 读 | **不拦**（元数据非数据行） |

## D4 状态码与顺序

- 队内无授权 = 403（`detail` 含数据集名与「无访问授权」），存在性不隐藏——队内
  成员本就看得见「有一张受限表」，隐藏反而造成管理困惑；
- 跨企业 = 404（既有 `_owned_dataset_or_none` 语义，逐字不变）；
- 顺序：租户隔离 404 → 档位闸 403（既有文案）→ ACL 403 → 业务校验。既有测试
  对档位 403 的断言零漂移（ACL 只在 `restricted` 数据集上生效，默认 team 直通）。

## D5 管理面：owner-only，整表替换

- `GET /api/v1/datasets/{id}/access` → `{mode, grants:[{logto_sub, permission}]}`；
- `PUT .../access` body `{mode, grants}`：整表替换语义（幂等），`mode=team` 时
  grants 清空；`logto_sub` 必须 ∈ 本企业成员 ∪ owner（否则 400 如实，含未知 sub 数）；
  viewer/editor 403（owner-only，沿密钥物纪律）；
- 每次 PUT 落一条 `dataset_acl_events`；PUT 与授权表变更同事务。

## D6 不拦（v1 边界，如实写进 spec 与文档）

1. **`wdk-` data-key 轨道**：key 是 owner 铸出的客户级凭据，不承载成员身份——
   成员无法持有 key，故不构成越权通道；若未来给成员发 key，须同步扩 ACL（v2）。
2. **MCP 工具面**（wire-ops / wire 入口）：客户级凭据调用，同上。
3. **产物/看板快照**：快照已物化数据，属独立可见性域（artifact-marketplace 四档），
   v2 另裁「受限数据集的产物是否连带受限」。
4. **列级脱敏**：ROADMAP 明写延后不排期。

## D7 兼容与回归

- 默认 `team`：存量数据集、全部既有 api/web 测试逐字节等价（回归门=api 全量套件
  与 web 全量套件基线，不新增豁免）。
- 免费档：ACL 不改变档位判定；受限数据集在免费档同样 403（档位闸先行）。
- 迁移 0047 双方言（sqlite + PG 真链），`create_all` 兜底幂等（沿 0041–0045 容错化
  先例）。

## D8 实施时序（并发纪律）

本 change 与在途 `wire-entity-mn-materialize` 共享 `datasets/routes.py`、
`data_plane/*`：**代码实施在对方 commit 后开始**，避免同树半成态互相污染
（10-28 事故教训：未 commit 代码被滚动发布打包进生产）。
