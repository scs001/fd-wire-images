# Tasks: wire-cell-pg-provisioning

## 1. cell-manager 实现

- [x] 1.1 `cell_manager/openbao.py`：KV v2 stdlib 客户端（put/get/delete，
  X-Vault-Token；封印 503/403/不可达 → OpenbaoError 带解封指引；无本地降级）
- [x] 1.2 `cell_manager/storage_pg.py`：PostgresStorage（provision 七步含
  隔离验证四查与自愈轮换；teardown_created 只删本次新建；destroy 幂等
  DROP DATABASE WITH FORCE + DROP ROLE）；psycopg 可选导入缺依赖报可行动
  错误；口令 secrets.token_hex(24) 只走内存/OpenBAO/env
- [x] 1.3 `config.py`：CELL_STORAGE/sqlite 默认/非法值拒；postgres 画像
  必需项（pg_admin_url/pg_url_host/port/openbao_addr/token）缺失 ConfigError
- [x] 1.4 `manager.py`：provision 接入（env 覆盖+回滚 teardown_created+审计
  storage.provisioned 白名单字段）；purge 接 destroy；state record 增
  storage 字段；deprovision 不动库；sqlite 画像零触达

## 2. 测试

- [x] 2.1 单元：fake 矩阵（provision/幂等/回滚/purge/ConfigError/OpenBAO
  三态 fake http）
- [x] 2.2 集成腿（env 门控）：真 PG+真 OpenBAO 全链（建/重入/自愈轮换/
  隔离四查/teardown/destroy，前缀 wire_cell_test_ 用后即删）
- [x] 2.3 既有测试套零回归（sqlite 画像不动为门）

## 3. 配方与部署面

- [x] 3.1 `deploy/wire-postgres/backup.sh` 动态发现 wire_cell_%；重部署
  cheap-3 并验证备份产物含 wire_cell_drillcust001
- [x] 3.2 `docs/cell-postgres-storage.md` 增「自动开通」节（env/psycopg
  extra/OpenBAO 解封运维与 token 铸造配方/purge 语义/试点记录锚点）
- [x] 3.3 pyproject：`postgres` extra（psycopg[binary]>=3.1）

## 4. 试点（生产 e2e）

- [x] 4.1 cheap-1 /opt/cell-manager 更新（rsync+psycopg 安装）+ env.sh 增量
  （CELL_STORAGE=postgres、admin URL、OpenBAO addr/token 从
  /root/wire-cell-openbao.token）
- [x] 4.2 全自动开通 `pgauto001`：库/角色/ACL/隔离四查/OpenBAO 条目/容器
  PG 根 healthy/registry 注册，全程零手工 SQL
- [x] 4.3 容器内 MCP 快速冒烟（initialize+工具面+建集/查数/删除）+
  回退面验证（purge 语义留观察，不执行）
- [x] 4.4 幂等重入实证（二次 provision ACTIVE no-op）与凭据零泄露抽查
  （audit/state/日志 grep 无明文）

## 5. 收口

- [x] 5.1 `openspec validate wire-cell-pg-provisioning --strict` 过；全任务
  勾选后 archive
- [x] 5.2 ROADMAP 方向 A 标注 v2；记忆更新（含 OpenBAO 解封事故与正确坐标
  cheap-1:8200、token 铸造配方）
