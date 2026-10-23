# wire-postgres 生产实例部署记录（cheap-3，2026-10-11）

用户裁决：cheap-1 空间不足，PG 落 cheap-3（Jenkins 退役后余量释放）。

## 实况与 compose 配方的偏差（均为环境适配，语义对齐）

| 配方（deploy/wire-postgres） | cheap-3 实况 | 原因 |
|---|---|---|
| docker compose | `docker run` + restart=unless-stopped | cheap-3 无 compose（GitHub 直连超时装不了插件） |
| named volume wire_pg_data | host 目录 /opt/wire-postgres/data | 同等持久化；宿主路径便于 pg_dump/搬运 |
| 绑定 127.0.0.1:5433 | 绑定 **100.64.0.12:5433**（tailscale） | api 在 cheap 集群（cheap-6），跨机访问走 tailnet；不对公网暴露 |
| mem_limit 2g | --memory 768m --cpus 1.5 | cheap-3 可用内存 1.1G（与 dl-caddy/s3cache 共机）谦让 |
| backup.sh（compose exec） | docker exec 变体（同目录 backup.sh） | 同语义：两库分文件 gzip、任一失败非零退出、保留清理 |

## 部署验证（全部实证）

- 镜像 postgres:16-trixie（钉版）；容器 `Up (healthy)`，healthcheck=pg_isready
- 双库齐：`wire_platform`（POSTGRES_DB 建）+ `wire_datasets`（initdb 脚本建）✓
- shm_size 512m ✓；log 轮转 50m×5 ✓；TZ=UTC ✓
- 凭据：/opt/wire-postgres/.env（0600，不入 git）
- 备份：cron `30 3 * * *`（UTC）→ backups/ 两库分文件；首跑产出 20261010-183154 双文件
- **恢复演练**：restore_test 库建→gunzip|psql 灌入→管道零错→drop ✓

## 集群侧连通性（api 侧使用方式，迁移 cutover 时）

cheap 集群节点（含 cheap-6）经 tailnet 访问 `postgresql+psycopg://wire:***@100.64.0.12:5433/wire_platform`（与 wire_datasets 同实例分库）。生产 cutover 按仓内 README「生产迁移与回退手册」走（备份→建库→alembic→逐表对账→切 env；对账不过即止）——**本日未执行 cutover，api 生产仍跑 SQLite PVC**，留作独立窗口。
