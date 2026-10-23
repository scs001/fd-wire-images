# Proposal: wire-cell-pg-provisioning — cell-manager 自动「建库+发凭据」（cell-postgres-storage v2）

## Why

v1（2026-10-12 归档）交付了引擎可选 PG 数据根与手工配方（建库/属主/隔离验证/
迁移/回退），并把 drillcust001 试点搬上 PG。但每开一个新 cell 仍要手工跑
六步 SQL+传凭据+改 backup 清单——自动化欠账即 ROADMAP 方向 A 原文「cell-manager
增加『建库+发凭据』步骤（凭据入 OpenBAO）」。生产 OpenBAO（cheap-1
mcp-openbao，registry 栈成员）已具备条件：本轮已解封（此前封印 5 小时致
registry 密存降级）并为 cell-manager 铸好仅限 `secret/wire/cell-db/*` 的
10 年期专用 token。

## What Changes

1. **cell-manager 存储画像**：`CELL_STORAGE`（`sqlite` 默认｜`postgres`）。
   postgres 时 provision 在建卷后自动：建属主角色+一 cell 一库+REVOKE PUBLIC
   → 隔离验证（has_database_privilege）→ 口令生成（secrets）→ **凭据写入
   OpenBAO**（`secret/wire/cell-db/<customer_id>`，KV v2）→ 容器 env 注入
   `DAAS_DATABASE_URL`（审计只留 env 名与库名，不落明文）。幂等重入：库已在
   则验 ACL+从 OpenBAO 取凭据；OpenBAO 条目缺失则轮换口令自愈（ALTER ROLE +
   重写条目）。provision 失败回滚含「本次新建的库/角色」；**purge（保留期
   满）删库+删角色，deprovision 保留库**（与卷语义对齐）。
2. **sqlite 路径零变化**：`CELL_STORAGE` 未设或 sqlite 时行为字节不变（现有
   测试零改动全绿为门）。
3. **backup.sh 动态发现**：`deploy/wire-postgres/backup.sh` 从硬编码清单改为
   动态发现 `wire_cell_%` 库（根治「新 cell 库不进备份」的设计债），并重部署
   cheap-3。
4. **配方文档**：`docs/cell-postgres-storage.md` 增「自动开通」节——env 配置、
   OpenBAO 依赖与解封运维（封印=密封态是特性，开通要求解封态，fail-closed
   如实报错）、专用 token 铸造配方、purge 语义。

### 红线落地

- 凭据只进 OpenBAO 与容器 env（0600 纪律），不落日志/审计/state/镜像/git；
- OpenBAO 专用 token 策略仅限 `wire/cell-db/*` 路径（越路径拒）；
- 隔离验证成为自动开通的**必经步骤**（验证不过=开通失败回滚），不再依赖人
  工执行。

### v2 边界（显式不做）

- 存量 sqlite cell 的逐户搬迁编排（生产 wire cell 目前仅 drill 且已在 PG）；
- OpenBAO 自动解封/高可用（封印态运维属 registry 栈既有课题）；
- `pg_database_size` 商业配额重估（独立 change）。

## Impact

- **fd-wire/cell-manager**：新模块 storage_pg.py、config/provision/purge/
  state 接入、psycopg 可选依赖（extra `postgres`，仅 postgres 画像导入）、
  OpenBAO 客户端（stdlib HTTP，无新硬依赖）；测试=既有套件零回归 + 新存储
  测试（单元 fake + 真 PG/真 OpenBAO 集成腿，env 门控）。
- **fd-wire/deploy**：backup.sh 动态化。
- **生产**：cheap-1 /opt/cell-manager 更新 + env.sh 增量；cheap-3 backup.sh
  重部署；试点新 cell `pgauto001` 全自动开通 e2e 实证。
