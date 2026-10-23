# Design: wire-dedicated-postgres

## Context

拍板背景见 proposal。既有事实：api 有单一 alembic 链（`api/alembic/`）；datasets
模块独立 `FD_WIRE_DATASETS_DATABASE_URL`（生产 `/tmp/fd_wire.db`）；舰队 PG 配方
（fd-postgres：pg16-trixie、shm 512m、psycopg3、count FILTER、JSONB）。

```
cheap-1
┌─ wire-postgres（docker compose，pg16-trixie 钉版）
│    ├ database: wire_platform   ← api 元数据（customers/plans/metering/…）
│    └ database: wire_datasets   ← 数据集库（登记/同步/修正层 + api 层行存储）
└─ cells（docker 直跑，数据根仍 SQLite——独立 change 再迁）
```

## Goals / Non-Goals

**Goals:**
- 配方即可执行的部署/备份/恢复/迁移/回退五手册
- api 代码双方言为常态：SQLite（dev/测试）与 PG（生产）同套模型与迁移
- 元数据与数据集库可分别指向不同 PG database（爆炸半径与备份节奏解耦）

**Non-Goals:**
- cell 引擎数据根迁 PG（DAAS_PROFILE 存储画像，独立 change）
- 高可用/流复制（起步单实例+认真备份；HA 后置）
- 生产滚动执行（用户确认后按手册走）
- OpenBAO 凭据托管（先 env/.env，接 OpenBAO 独立任务）

## Decisions

- **D1 两 database 而非一**：`wire_platform`（元数据，小而关键）与
  `wire_datasets`（含行存储，大而可重建性强）分库——备份节奏、恢复演练、
  未来容量路径不同；连接串即边界。
- **D2 钉版 pg16-trixie**：舰队既有教训（collation 降级）直接继承；镜像 tag
  写死在 compose，升级走显式变更。
- **D3 psycopg3 驱**：`psycopg[binary]` 钉版进 api 依赖（SQLAlchemy 2.x 下
  psycopg2 已知断）；PG URL 形如 `postgresql+psycopg://…`。
- **D4 双方言审计清单**：JSON 列（PG 用 JSONB、查询用 SQLAlchemy JSON path
  抽象）、布尔聚合一律 `count(*) FILTER`（PG 无 max(boolean)）、时间统一
  naive-UTC 既有约定、自增主键用 Integer Identity 兼容形态；审计产出清单文件
  （reports/）逐项修复并附测试。
- **D5 dev 默认不变**：未设 URL 时回落 SQLite 文件——双方言靠测试矩阵保证
  （每模块测试跑 SQLite；PG 冒烟=CI/本地有实例则真连，否则方言编译断言），
  不靠"开发也用 PG"。
- **D6 迁移手册含计数对账**：SQLite→PG 搬运后逐表 count 对账 + 关键聚合抽查
  （当月 metering 行数、数据集行数）；回退=切回旧 env + 旧库只读保留。

## Risks / Trade-offs

- [单实例共因故障] → 每日备份 + 恢复演练文档 + 监控健康检查；HA 后置是显式取舍
- [双方言长期漂移] → PG 冒烟进测试矩阵（有实例即真连），审计清单沉淀为回归用例
- [生产 /tmp 旧库搬迁窗口] → 手册规定低峰执行 + 旧库只读保留一周期

## Migration Plan

1. 配方与手册（compose/备份/恢复/迁移/回退）落 deploy/
2. api config 双 URL 收敛 + psycopg3 依赖
3. 双方言审计与修复（含 free-tier-data-gate 新表）
4. 本地/CI PG 冒烟接进测试矩阵
5. 生产滚动（运维步骤，另行确认后执行）

## Open Questions

- 备份保留策略（每日×保留 14 天起步？）——手册给默认值，运维可调
