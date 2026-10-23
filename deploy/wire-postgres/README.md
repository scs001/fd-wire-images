# wire-postgres 运维手册（柏讯专用 PostgreSQL）

> wire-dedicated-postgres：柏讯是独立产品条线，配专用 PG。生产部署目标
> **cheap-1**（与 cells 同机，就近低延迟）。cell 引擎数据根迁 PG 不在本
> 配方范围（独立后续 change）。

拓扑（design D1）：

```
cheap-1
┌─ wire-postgres（docker compose，pg16-trixie 钉版）
│    ├ database: wire_platform   ← api 元数据（customers/plans/metering/…）
│    └ database: wire_datasets   ← 数据集库（登记/同步/修正层 + api 层行存储）
└─ cells（docker 直跑，数据根仍 SQLite——独立 change 再迁）
```

api 侧双 URL（`api/fd_wire_api/config.py`）：

| env | 语义 | 未设时 |
|---|---|---|
| `FD_WIRE_DATABASE_URL` | 元数据库（wire_platform），全平台模块收敛开关 | 各模块回落既有 SQLite 文件（dev 不变） |
| `FD_WIRE_DATASETS_DATABASE_URL` | 数据集库（wire_datasets），datasets/provenance 切片 | datasets 默认 SQLite 文件 |

PG URL 形态：`postgresql+psycopg://wire:<口令>@<host>:5433/wire_platform`。
历史形态（`postgres://`、`postgresql://`、`postgresql+psycopg2://`）会被
api 自动改写为 `postgresql+psycopg://`（SQLAlchemy 2.x 下 psycopg2 已知断，
不装 psycopg2）。

## 一、部署

```bash
cd deploy/wire-postgres
cp env.example .env          # 填 POSTGRES_PASSWORD 等真实值（.env 永不入 git）
chmod +x backup.sh initdb/01-create-databases.sh
docker compose up -d         # 首次起：initdb 脚本建 wire_datasets；幂等
docker compose ps            # 等 healthy
# 冒烟：两库都在
docker compose exec wire-postgres psql -U wire -d wire_platform -c 'select 1'
docker compose exec wire-postgres psql -U wire -d wire_datasets -c 'select 1'
```

api 切换（建表走 alembic，见「五、生产迁移手册」；一次性验证可先开
`create_all` 兜底——生产应设各 `*_AUTO_CREATE_TABLES=0` 走迁移链）。

cron（每日 03:30 UTC 备份）：

```
30 3 * * * cd /opt/wire-postgres && ./backup.sh >> backups/backup.log 2>&1
```

## 二、升级（显式变更）

镜像 tag 钉死在 `docker-compose.yml`（`postgres:16-trixie`）。升级纪律：

1. **小版本（16.x→16.y）**：改 tag → `docker compose pull && docker compose up -d`。
   trixie 基座内小版本升级无 collation 变更风险；起后看日志无 ERROR、
   `pg_isready` 健康、两库可查即收。
2. **大版本（16→17）**：必须 `pg_dumpall` 全量备份 → 新数据卷新实例 →
   恢复演练通过 → 再切；绝不 `pg_upgrade` 裸跳（舰队教训：collation
   降级会让文本索引静默损坏）。
3. 任何升级先跑一遍 `./backup.sh` 确认备份文件可恢复（见「四」）。

## 三、监控要点

- **健康检查**：compose healthcheck（`pg_isready`，10s 间隔）；接入宿主
  告警：`docker inspect --format '{{.State.Health.Status}}' wire-postgres`
  非 `healthy` 即告警。
- **连接饱和**：`SELECT count(*) FROM pg_stat_activity;` 接近
  `max_connections`（默认 100）时排查长事务：
  `SELECT pid, now()-xact_start AS age, state, query FROM pg_stat_activity WHERE state <> 'idle' ORDER BY age DESC;`
- **磁盘**：数据卷 `wire_pg_data` 与 `./backups` 各自的占用告警（80%）；
  备份目录膨胀先查保留天数是否被改大。
- **慢查询**：开发期可开 `log_min_duration_statement=1000`（compose
  environment 加一行）观察同步推送/结算聚合的耗时基线。
- **复制/HA**：起步单实例（design 非目标），认真备份是唯一防线——
  每日备份失败（backup.sh 非零退出）必须当天处理。

## 四、备份与恢复

### 备份

`./backup.sh`：两库分文件 `backups/<db>-<UTC时间戳>.sql.gz`，保留
`WIRE_PG_BACKUP_RETENTION_DAYS`（默认 14 天）。任一库 dump 失败脚本即
非零退出（cron 日志可见）。

### 恢复到任一备份点（命令序列）

```bash
cd deploy/wire-postgres
STAMP=20261011-033000            # 目标备份点（文件名中段）
docker compose stop wire-postgres-app 2>/dev/null || true   # 先停写入方（api）
# 1) 重建目标库（drop 前再次确认连的是 wire-postgres 而非别的实例！）
docker compose exec wire-postgres psql -U wire -d postgres -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='wire_platform';"
docker compose exec wire-postgres psql -U wire -d postgres -c 'DROP DATABASE IF EXISTS wire_platform;'
docker compose exec wire-postgres psql -U wire -d postgres -c 'CREATE DATABASE wire_platform;'
# 2) 灌备份
gunzip -c backups/wire_platform-${STAMP}.sql.gz | \
  docker compose exec -T wire-postgres psql -U wire -d wire_platform
# wire_datasets 同法（库名换成 wire_datasets）
# 3) 恢复写入方（api），核对计数
```

### 恢复演练（每季度至少一次，不占生产库）

```bash
STAMP=$(ls backups/wire_platform-*.sql.gz | tail -1 | sed 's/.*-\([0-9-]*\)\.sql\.gz/\1/')
docker compose exec wire-postgres psql -U wire -d postgres -c 'CREATE DATABASE wire_platform_restore_test;'
gunzip -c backups/wire_platform-${STAMP}.sql.gz | \
  docker compose exec -T wire-postgres psql -U wire -d wire_platform_restore_test
docker compose exec wire-postgres psql -U wire -d wire_platform_restore_test \
  -c "SELECT count(*) FROM metering_records;"     # 与生产同口径计数比对
docker compose exec wire-postgres psql -U wire -d postgres -c 'DROP DATABASE wire_platform_restore_test;'
```

演练要点：restore_test 库能建、能灌、关键表计数与生产一致、DROP 干净。
任何一步失败=备份不可信，当天修复备份链。

## 五、生产迁移手册（SQLite → PG）

> 低峰执行（建议 02:00–05:00 UTC）；全程旧库不动（只读镜像源），
> 对账不过**即止**，不切 env。

### 5.1 预备

```bash
# 旧库全量备份（SQLite 侧=拷文件，先 wal_checkpoint 落盘）
sqlite3 /path/to/fd_wire_platform.db 'PRAGMA wal_checkpoint(TRUNCATE);'
cp /path/to/fd_wire_platform.db /path/to/fd_wire_platform.db.bak-$(date -u +%Y%m%d)
cp /path/to/fd_wire_datasets.db  /path/to/fd_wire_datasets.db.bak-$(date -u +%Y%m%d)
```

### 5.2 建库（若尚未部署实例，先走「一、部署」）

`wire_platform` / `wire_datasets` 两库就绪（initdb 脚本已建）。

### 5.3 建表（alembic 迁移链）

```bash
cd api
FD_WIRE_DATABASE_URL=postgresql+psycopg://wire:<口令>@127.0.0.1:5433/wire_platform \
  uv run alembic upgrade head
FD_WIRE_DATASETS_DATABASE_URL=postgresql+psycopg://wire:<口令>@127.0.0.1:5433/wire_datasets \
  uv run alembic -x db_url=postgresql+psycopg://wire:<口令>@127.0.0.1:5433/wire_datasets upgrade head
```

（`alembic/env.py` 优先级：`-x db_url` > `FD_WIRE_DATABASE_URL` >
`DATABASE_URL`；数据集库经 `-x db_url` 显式指定。）

### 5.4 数据搬运（逐表 + 计数对账 + 关键聚合抽查）

逐表 `SQLite SELECT → PG COPY/INSERT`。示例（metering_records，其余表同法；
Python 侧统一脚本可基于 `api` 依赖的 SQLAlchemy 双引擎读旧写新）：

```bash
cd api && uv run python - <<'EOF'
import sqlite3
import psycopg
# 每表：读旧（SQLite）→ executemany 写新（PG），行数两侧对账
# 旧库路径来自生产 PVC；新库 URL 来自 .env（勿入命令行历史）
EOF
```

**对账清单（全部通过才允许切 env）**：

1. 逐表计数：两侧 `SELECT count(*) FROM <table>;` 完全一致（含
   alembic_version）。
2. 关键聚合抽查：
   - 当月 metering 行数：
     `SELECT count(*), sum(rows_returned) FROM metering_records WHERE to_char(event_timestamp,'YYYY-MM') = to_char(now(),'YYYY-MM');`
     （SQLite 侧等价：`WHERE strftime('%Y-%m', event_timestamp) = strftime('%Y-%m','now');`）
   - 数据集行数：`datasets` 表计数 + api 层行存储 `dataset_rows` 计数
     （wire-free-tier-data-gate 表，集成后以并行代理最终表名为准）。
   - billing：`billing_customers` / `billing_ledger_entries` 计数与末笔
     `created_at` 一致。
3. 抽样逐行比对：每表随机 3 行（主键对齐）字段级一致（JSON 列经
   `json.loads` 归一后比较——SQLite 存文本、PG 存 json，序列化差异不较真）。

**对账不过即止**：不切 env、不动旧库、修复后从头重搬（PG 侧可
TRUNCATE 重灌）。

**可选优化（对账通过后、切 env 前做一次即可）——JSON 列转 JSONB**：
ORM 经 SQLAlchemy `JSON` 抽象（双方言常态），PG 迁移链建出的是 `json`
类型；`jsonb` 的索引/更新性能更好（design D4 意图），转换语句：

```bash
# 对 wire_platform 与 wire_datasets 各 JSON 列执行（列清单见
# reports/dialect-audit.md「JSON 列清单」）
docker compose exec wire-postgres psql -U wire -d wire_platform -c \
  "ALTER TABLE demands ALTER COLUMN entities_hint TYPE jsonb USING entities_hint::jsonb;"
```

ORM 不改类型（`sqlalchemy.JSON` 对 `jsonb` 列读写同样成立），避免与冻结
迁移链漂移；转换幂等可回查 `\d <table>`。

### 5.5 切 env

api 部署面（k8s Secret `fd-wire-env` / ArgoCD 同步）只改两条：

```
FD_WIRE_DATABASE_URL=postgresql+psycopg://wire:<口令>@<host>:5433/wire_platform
FD_WIRE_DATASETS_DATABASE_URL=postgresql+psycopg://wire:<口令>@<host>:5433/wire_datasets
AI_DATABASE_URL=postgresql+psycopg://wire:<口令>@<host>:5433/wire_platform   # ai 切片独立 env 名，指向同一元数据库
```

滚动重启 api（ArgoCD 同步即滚动）。

### 5.6 验证清单（切 env 后 30 分钟内逐项过）

- [ ] `/health` 200；
- [ ] 控制台登录 + 首登开户（console_onboardings 正常写入）；
- [ ] 提交一张需求单（demands 写入）；
- [ ] 触发一次目录同步（catalog 镜像行数>0）；
- [ ] 数据集列表/上传（datasets + api 层行存储）；
- [ ] 内部 usage 聚合端点返回非空（metering 聚合在 PG 上口径正确）；
- [ ] `pg_stat_activity` 无长事务堆积；
- [ ] 次日备份文件出现且 restore_test 演练通过。

## 六、回退配方

**触发条件**：验证清单任一项无法在 1 小时内修复。

1. 切回旧 env：`FD_WIRE_DATABASE_URL` / `FD_WIRE_DATASETS_DATABASE_URL` /
   `AI_DATABASE_URL` 改回原 SQLite 路径（PVC 内），滚动重启 api。
2. **旧库只读保留一个完整周期（≥7 天）**：期间旧库不写入（回退后新写
   落回旧库——只读保留指「不再迁移/删除」，业务写自然恢复）。
3. PG 侧新库保留作取证与差异核对：回退窗口内写进 PG 的增量行，按
   主键与时间戳整理补录回 SQLite（通常为 0——低峰切换窗口）。
4. **再次前滚注意**：重跑 5.4 搬运前先 `TRUNCATE` PG 侧目标表（或
   drop/recreate database 后重跑 alembic），避免主键冲突；上次对账输出
   可复用作快速回归。
5. 一周期后确认无回退需求，走「七」处置旧库。

## 七、/tmp 旧库处置（归档与保留期）

生产现状 `/tmp` 共享 SQLite（已知脆弱点）搬迁完成后：

```bash
# 1) 归档（挂载点与保留期按 cheap-1 盘况定，建议冷存储 ≥90 天）
mkdir -p /var/archive/fd-wire-sqlite/$(date -u +%Y%m%d)
mv /tmp/fd_wire.db* /var/archive/fd-wire-sqlite/<日期>/
# 2) 留 README 一行：库名、搬迁日期、对应 PG 备份戳、保留期至
# 3) 一周期（回退窗口）内不删；期满删除前最后跑一次备份脚本
```

注意：`/tmp` 在宿主重启后本就可能被清——这正是迁 PG 的动机之一；
归档是补救历史数据的最后机会，搬迁日必做。

## 八、凭据管理现状

- 当前：`.env`（本目录，源自 `env.example`）+ api 侧 k8s Secret
  `fd-wire-env`。口令永不入 git、不入命令行历史（用 `docker compose
  exec`/Secret 注入而非 `-p` 明文）。
- 改口令：`docker compose exec wire-postgres psql -U wire -c "ALTER USER wire WITH PASSWORD '<新口令>';"`，
  同步改 .env 与 k8s Secret，滚动 api。
- **OpenBAO 接管为后续独立任务**：接管后 .env 只留 `BAO_ADDR` 与
  AppRole 凭据，PG 口令改由 OpenBAO 动态下发（本配方不动）。
