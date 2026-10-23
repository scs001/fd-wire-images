#!/usr/bin/env bash
# wire-postgres 每日备份（wire-dedicated-postgres 任务 1.2）。
#
# 两库分文件 gzip 到 ./backups/，保留天数 WIRE_PG_BACKUP_RETENTION_DAYS
# （默认 14，env 可配）。挂 cron：每日 03:30 UTC——
#   30 3 * * * cd /opt/wire-postgres && ./backup.sh >> backups/backup.log 2>&1
# 恢复演练步骤见同目录 README「备份与恢复」。
set -euo pipefail

cd "$(dirname "$0")"

# 实器名 wire-postgres（部署主机无 compose 插件——docker exec 等价形态，
# 与 cheap-3 在役部署版一致）；凭据来自容器 env（与实例同源，不在脚本里复制口令）
POSTGRES_USER="$(docker exec wire-postgres printenv POSTGRES_USER)"
POSTGRES_DB="${POSTGRES_DB:-wire_platform}"
RETENTION_DAYS="${WIRE_PG_BACKUP_RETENTION_DAYS:-14}"

STAMP="$(date -u +%Y%m%d-%H%M%S)"
OUT_DIR="backups"
mkdir -p "$OUT_DIR"

# 库清单 = 平台两库 + 动态发现的全部 wire_cell_% 库（一 cell 一库，
# wire-cell-pg-provisioning：新 cell 库自动进备份，无需改清单）。
# 任一库失败即整体失败（备份宁错杀不静默）。
CELL_DBS="$(docker exec wire-postgres \
    psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
    "select datname from pg_database where datname like 'wire_cell_%' order by datname")"
for db in "$POSTGRES_DB" wire_datasets $CELL_DBS; do
    echo "[$(date -u +%FT%TZ)] backing up ${db} ..."
    docker exec wire-postgres \
        pg_dump -U "$POSTGRES_USER" --no-owner --no-privileges "$db" \
        | gzip -9 > "${OUT_DIR}/${db}-${STAMP}.sql.gz"
done

# 保留策略：清理超过保留天数的备份（对两库一视同仁）
find "$OUT_DIR" -name '*.sql.gz' -mtime "+${RETENTION_DAYS}" -delete

echo "[$(date -u +%FT%TZ)] backup done (retention ${RETENTION_DAYS}d):"
ls -lh "$OUT_DIR"/*.sql.gz | tail -4
