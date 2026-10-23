# cell-postgres-storage Specification（delta）

## ADDED Requirements

### Requirement: cell-manager 自动建库发凭据

`CELL_STORAGE=postgres` 时，cell-manager 开通（provision）MUST自动完成
一 cell 一库供给：建属主角色与 `wire_cell_<customer_id>` 库、`REVOKE CONNECT
… FROM PUBLIC`、隔离验证、生成口令、凭据入 OpenBAO（`secret/wire/cell-db/
<customer_id>`，KV v2）、容器 env 注入 `DAAS_DATABASE_URL`。全程MUST不把
凭据明文落入日志、审计、state 或镜像。`CELL_STORAGE` 未设或为 sqlite 时
行为MUST与现状字节一致。

#### Scenario: 全自动开通
- **WHEN** 对新客户以 postgres 画像执行 provision
- **THEN** 库/角色/ACL 就位、隔离验证通过、OpenBAO 条目写入、容器以 PG
  数据根就绪，返回信息含库名与注入的 env 名（不含明文凭据）

#### Scenario: 幂等重入
- **WHEN** 对已 PG 化客户重复 provision（或 ACTIVE 前失败重试）
- **THEN** 不重复建库；ACL 校验仍在；凭据从 OpenBAO 读取复用；OpenBAO
  条目缺失时轮换口令自愈（ALTER ROLE+重写条目）

#### Scenario: 失败零半写
- **WHEN** 供给链任一步失败（建库/验证/OpenBAO/容器）
- **THEN** 本次新建的库与角色被回滚清理，不留孤儿；既有资产不受损

### Requirement: 凭据入 OpenBAO 且令牌受限

cell 数据根凭据MUST以 OpenBAO 为唯一持久存放（容器 env 为运行期注入）；
cell-manager 使用的 tokenMUST受 ACL 策略限定仅可访问 `wire/cell-db/*`
路径族（越路径拒绝）。OpenBAO 处于封印态时开通MUST fail-closed 并如实
报错（提示先解封），不得静默降级为本地明文存放。

#### Scenario: 越路径拒绝
- **WHEN** 以 cell-manager token 尝试读写 `wire/cell-db/*` 以外的路径
- **THEN** 被拒绝（permission denied）

#### Scenario: 封印 fail-closed
- **WHEN** OpenBAO 封印或不可达时执行 postgres 画像 provision
- **THEN** 开通失败并明确报错（含解封指引），不产生任何本地凭据落盘

### Requirement: 生命周期语义与备份覆盖

purge（保留期满清退）MUST删除该 cell 的 PG 库与属主角色（与卷清除对齐）；
deprovision（退租保留期）MUST保留库（与卷保留对齐）。备份脚本MUST动态
发现 `wire_cell_%` 前缀的全部库并逐库备份——新增 cell 库无需人工改清单。

#### Scenario: purge 删库
- **WHEN** 对 postgres 画像 cell 执行 purge
- **THEN** `wire_cell_<id>` 库与角色被删除，卷清除照旧，审计留痕

#### Scenario: 备份自动覆盖新库
- **WHEN** 新建任意 `wire_cell_*` 库后运行备份脚本
- **THEN** 该库被自动纳入备份产物，无需修改脚本
