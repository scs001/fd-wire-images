## Purpose

定义柏讯专用 PostgreSQL 存储：实例配方（钉版/卷/备份/恢复）、api 双 URL 语义
（元数据与数据集库分离且均可指 PG）、模型与迁移的 SQLite+PG 双方言保障，以及
生产迁移与回退手册——使 api 侧存储摆脱多 SQLite 文件与 /tmp 脆弱点，为数据
中台规模地基（含免费档 api 层行存储）就位。

## ADDED Requirements

### Requirement: 专用实例配方

柏讯提供可执行的 wire 专用 PG 实例配方：pg16-trixie 钉版、shm 512m、独立持久
卷、健康检查；元数据与数据集分属两个 database（wire_platform / wire_datasets）；
升级换版为显式变更（改 tag），不静默漂移。

#### Scenario: 配方可起
- **WHEN** 按 deploy/wire-postgres 文档在 docker 环境执行
- **THEN** 实例就绪、两库存在、健康检查通过，重跑幂等

### Requirement: 备份与恢复

每日对两库分别产出备份文件（保留策略可配），恢复手册给出恢复到任一备份点的
命令序列并经过演练验证。

#### Scenario: 备份可恢复
- **WHEN** 执行备份脚本后按恢复手册演练
- **THEN** 两库可恢复至备份点且数据完整

### Requirement: api 双 URL 语义

api 存储由 `FD_WIRE_DATABASE_URL`（元数据）与 `FD_WIRE_DATASETS_DATABASE_URL`
（数据集库）两个 URL 定义，均可指向 PostgreSQL（psycopg3 驱）；未配置时回落
SQLite 文件——开发与测试的默认行为不变。

#### Scenario: 切换后端
- **WHEN** 把两个 URL 从 SQLite 换成 PG 连接串并重启
- **THEN** 建表迁移与应用启动照常，无需代码分叉

### Requirement: 双方言保障

全部模型与迁移在 SQLite 与 PG 两方言下可建可查：JSON 列经 SQLAlchemy 抽象
（PG JSONB）、布尔聚合一律 count FILTER、时间统一 naive-UTC；双方言保障以测试
矩阵落地（有 PG 实例则真连，否则方言编译断言兜底）。

#### Scenario: 同套迁移两方言
- **WHEN** 对同一 alembic 链分别在 SQLite 与 PG 上执行 upgrade
- **THEN** 两边全部表结构建成且核心 CRUD 用例通过

### Requirement: 生产迁移与回退手册

生产 SQLite→PG 搬迁有手册：备份→建库→迁移→数据搬运（逐表计数对账+关键聚合
抽查）→切环境变量→验证清单；回退=切回旧 URL 且旧库只读保留一周期；滚动执行
为独立运维步骤。

#### Scenario: 对账不过即止
- **WHEN** 数据搬运后某表计数对账不一致
- **THEN** 手册规定中止切换、不切环境变量，旧库保持服务
