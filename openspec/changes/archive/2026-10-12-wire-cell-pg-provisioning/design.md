# Design: wire-cell-pg-provisioning

## 1. 现状锚点（2026-10-12 侦察）

- cell-manager（fd-wire/cell-manager，~2000 行纯标准库 + optional docker）：
  `Manager.provision` = 建卷（init_volume）→ env 装配（DAAS_DATABASE_URL
  写死 sqlite 四斜杠）→ ContainerSpec → create/start/wait_ready/register，
  异常回滚删容器+删空卷。CLI `python -m cell_manager provision <id>`。
  config 全部 `CELL_*` env（`CellManagerConfig.from_env`，测试可注入 mapping）。
- 生产部署=cheap-1 /opt/cell-manager（rsync 形态，非 git checkout），env.sh
  + state/audit/volumes/secrets 目录；生产 wire cell 仅 drillcust001（已在
  PG 终态，v1 手工迁移）。
- OpenBAO=cheap-1 `mcp-openbao`（openbao 2.5.5，registry 栈成员，file
  backend，8200/overlay 100.64.0.11:8200）。本轮已解封（此前封印 5h）、
  已铸专用 token（policy `wire-cell-db`，仅 `secret/data/wire/cell-db/*`
  CRUD+metadata，10y renewable，落 /root/wire-cell-openbao.token 0600）。
  解封钥+root token=/root/openbao-init-*.json（root-only，compose 注释
  即配方）。
- wire PG=cheap-3 `wire-postgres`（超管角色 `wire`，无 `wire` 库——admin
  连接串指 wire_platform）。

## 2. 方案

### 2.1 storage_pg 模块（新，cell_manager/storage_pg.py）

```python
class PostgresStorage:            # CELL_STORAGE=postgres 时由 Manager 持有
    def provision(customer_id) -> str        # → DAAS_DATABASE_URL（注入 env）
    def teardown_created(customer_id) -> None # provision 回滚：仅删本次新建资产
    def destroy(customer_id) -> None          # purge：删库+删角色（幂等）
```

provision 步骤（每步幂等、失败抛 `StorageProvisionError` 带上下文）：
1. admin 连接（psycopg，`CELL_PG_ADMIN_URL`）；
2. 角色存在？否 → `CREATE ROLE wire_cell_<id> LOGIN PASSWORD <secrets 48hex>`；
3. 库存在？否 → `CREATE DATABASE wire_cell_<id> OWNER …`；
4. `REVOKE CONNECT ON DATABASE … FROM PUBLIC`（新建与既有都执行）；
5. 隔离验证：`has_database_privilege` 四查（属主→本库 t、属主→平台库 f、
   无关角色→cell 库 f）任一不符 → 抛错（fail-closed，不自动修）；
6. OpenBAO：`GET secret/data/wire/cell-db/<id>`——有条目 → 复用口令；条目
   缺失但角色已存在（v1 手工迁移的 drill 即此形态）→ **轮换口令**
   （ALTER ROLE + PUT 条目）；新建角色 → PUT 条目（url/password/created_at）；
7. 返回 `postgresql+psycopg://wire_cell_<id>:<pw>@<host>:<port>/wire_cell_<id>`
   （host/port 来自 `CELL_PG_URL_HOST/PORT`，容器视角地址）。

口令纪律：`secrets.token_hex(24)`；只在内存与 OpenBAO/env 流转；审计事件
（`storage.provisioned`）只记 customer_id/库名/openbao 路径/env 名。

teardown_created/destroy 的 admin DDL：`DROP DATABASE (WITH (FORCE))` +
`DROP ROLE`（先 REASSIGN/DROP OWNED 由属主自库内对象天然满足——库整体
DROP 即随库消失；role 须先无对象依赖，库删后即无）。

psycopg 为可选依赖（pyproject `[project.optional-dependencies] postgres`），
仅 `CELL_STORAGE=postgres` 时 import，缺失时报可行动错误（pip install
cell-manager[postgres]）。

### 2.2 Manager/config 接入

- `CellManagerConfig`：+`storage`（CELL_STORAGE，默认 sqlite，非法值
  ConfigError）、+`pg_admin_url`、`pg_url_host/port`、`openbao_addr/token/
  path_prefix`——postgres 画像缺任一 PG/OpenBAO 必需项 → ConfigError
  fail-closed。
- `provision`：`db_path = init_volume(...)` 后，若 postgres 画像 →
  `url = storage.provision(...)`，`env["DAAS_DATABASE_URL"] = url`；异常
  回滚分支加 `storage.teardown_created`（尽力，原异常优先）。state record
  增 `storage` 字段（旧记录缺省=sqlite）。
- `purge`：record.storage==postgres → `storage.destroy`（在卷清除同批，
  失败如实抛——purge 是运维动作，不留静默半删）。
- `deprovision`：不动库（与卷保留对齐）。
- sqlite 画像：所有新代码路径不触达（`storage=None`），既有测试零改动。

### 2.3 OpenBAO 客户端（新，cell_manager/openbao.py）

stdlib `urllib` 直连 KV v2：`put/get/delete`（`X-Vault-Token`）；封印/
不可达/403 → `OpenbaoError` 带解封指引文案（fail-closed）。不做自动解封、
不做本地降级存放。

### 2.4 backup.sh 动态发现（deploy/wire-postgres/backup.sh）

```sh
for db in "$POSTGRES_DB" wire_datasets $(docker exec <pg> psql -U <user> \
  -d "$POSTGRES_DB" -tAc "select datname from pg_database \
  where datname like 'wire_cell_%' order by datname"); do
```

（容器内 psql 免密语义与现状一致；重部署 cheap-3 后跑一次验证产物含
wire_cell_drillcust001。）

### 2.5 测试

- 单元（默认跑）：fake storage/fake driver 的 provision·幂等·回滚·purge·
  ConfigError 矩阵；OpenBAO 客户端用本地 http server fake（封印 503/403/
  正常三态）。
- 集成腿（env 门控，缺 env 跳过并打原因）：`CELL_PG_TEST_ADMIN_URL`+
  `CELL_OPENBAO_TEST_ADDR/TOKEN` 指真库真 bao——真建/幂等重入/自愈轮换/
  隔离四查/teardown/destroy 全走真 DDL（测试库前缀 `wire_cell_test_`，
  用后即删）。发布门要求集成腿跑过（生产试点前的最后闸）。

## 3. 风险与对策

| 风险 | 对策 |
|---|---|
| OpenBAO 封印窗口阻断开通 | fail-closed+解封指引；封印=密封态是特性；运维配方入 docs |
| provision 半途失败留孤儿库 | teardown_created 只删「本次新建」（建前探测结果入内存） |
| DROP DATABASE 有活动连接 | `WITH (FORCE)`（PG13+，实例 16） |
| 凭据进日志/异常栈 | 口令只存局部变量；异常消息仅库名/步骤；审计字段白名单 |
| psycopg 不在目标机 | extra 安装配方入 docs；缺失时报 pip install cell-manager[postgres] |

## 4. 后续（不在本 change）

存量 sqlite cell 逐户搬迁编排、`pg_database_size` 配额杠杆、OpenBAO
auto-unseal/高可用、cell-manager 滚动发布通道化（现为 rsync 形态）。
