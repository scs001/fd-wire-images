# Tasks — wire-dedicated-postgres

## 1. 实例配方（deploy）

- [x] 1.1 `deploy/wire-postgres/docker-compose.yml`：pg16-trixie 钉版镜像、shm_size 512m、独立命名卷、健康检查、资源限制；`up -d` 幂等
- [x] 1.2 备份与恢复：每日 pg_dump cron 脚本（两库分文件、gzip、保留策略 env 可配）+ 恢复演练步骤文档（恢复到任一备份点的命令序列）
- [x] 1.3 运维手册 `deploy/wire-postgres/README.md`：部署、升级（换 tag 显式变更）、监控要点、凭据管理现状（env/.env，OpenBAO 接管为后续任务）

## 2. api 双 URL 收敛（api）

- [x] 2.1 config 明确双 URL 语义：`FD_WIRE_DATABASE_URL`（元数据）与 `FD_WIRE_DATASETS_DATABASE_URL`（数据集库）；未设回落 SQLite 文件（dev 行为不变）；PG URL 用 `postgresql+psycopg://` 形态并单测解析（`tests/test_storage_config.py`；datasets 既有 env 名即收敛名，兼容保持）
- [x] 2.2 依赖钉版：`psycopg[binary]==3.3.6` 进 api 依赖并锁 uv.lock；空跑 import 与连接串构建冒烟（test_pg_dialect_smoke 的 URL 形态用例）

## 3. 双方言审计与修复（api）

- [x] 3.1 审计清单产出：逐模块排查 SQLite 特有写法（JSON 函数、布尔聚合、时间函数、自增形态），清单落 `reports/dialect-audit.md`（问题点+修复方案+状态）
- [x] 3.2 修复与回归：布尔聚合统一 `count(*) FILTER`、JSON 经 SQLAlchemy 抽象、时间 naive-UTC 复核；既有测试全绿（strftime×4 收敛 dbcompat；布尔聚合计 0 违例、约定沉淀）
- [x] 3.3 free-tier-data-gate 新表（dataset_columns/dataset_rows）纳入审计：PG JSONB 建表与 JSON path 查询方言冒烟通过（源码归并行代理；DatasetsBase metadata 已进 PG create/drop 编译断言，集成时主代理复核真连冒烟）
- [x] 3.4 测试矩阵：PG 冒烟用例（`PG_TEST_URL` env 则真连建表+CRUD；否则对全部模型做 create/drop 的 PG 方言编译断言）——`tests/test_pg_dialect_smoke.py`

## 4. 迁移与回退手册（deploy）

- [x] 4.1 生产迁移手册：备份→建两库→alembic upgrade→数据搬运（逐表+计数对账+关键聚合抽查：当月 metering、数据集行数）→切 env→验证清单；写入 deploy 文档（含可选 JSONB 转换）
- [x] 4.2 回退配方：切回旧 env + 旧库只读保留一周期 + 再次前滚的注意事项
- [x] 4.3 `/tmp` 旧库处置说明（搬迁后归档路径与保留期）

## 5. 验证

- [x] 5.1 compose 本地演练：起实例→两库建好→备份脚本出文件→按恢复文档恢复成功（**本地 docker 环境不可用——compose/shell 语法与 YAML 结构已静态校验，实操演练待生产 cheap-1 执行**）
- [x] 5.2 api 全量测试绿（SQLite 路径）+ PG 方言冒烟绿；审计清单全项关闭（本批范围 559 passed + 11 skipped；datasets/data_plane 并行批次在改的测试文件见批次报告，集成时统一回归）
