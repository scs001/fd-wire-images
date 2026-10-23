#!/bin/bash
# 首次初始化：建第二库 wire_datasets（wire_platform 由 POSTGRES_DB 建）。
# 只在数据卷为空时执行一次（docker-entrypoint-initdb.d 语义）；幂等无害。
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    SELECT 'CREATE DATABASE wire_datasets'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'wire_datasets')\gexec
    GRANT ALL PRIVILEGES ON DATABASE wire_datasets TO "$POSTGRES_USER";
EOSQL
