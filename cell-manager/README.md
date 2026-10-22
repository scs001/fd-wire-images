# cell-manager（v1 已落地：任务 5.1）

企业 **cell**（隔离单元：独立容器进程 + 独立数据根，见 [ADR-0002](../docs/adr/0002-cell-is-the-enterprise.md)）的生命周期管理面。

- cell = 企业：一个容器实例 + 一个独立 SQLite WAL 数据卷（`daas.db`）+ 一条 registry server 条目（`fd-wire-cell-<customer_id>`，wire 入口同门计量）
- 平台侧**永不直读**租户 daas.db；跨租户数据一律住平台库
- 对 registry / 计量面只经 HTTP 借用契约（`CELL_REGISTRY_URL` 等，参照 `api/fd_wire_api/config.py` 纪律）

## 状态机

```
(不存在) --provision--> ACTIVE
ACTIVE   --pause------> PAUSED        （容器停，卷保留）
PAUSED   --resume-----> ACTIVE        （原卷重启）
ACTIVE|PAUSED --deprovision--> RETENTION（purge_at = now + CELL_RETENTION_DAYS，默认 30 天）
RETENTION --cancel-deprovision--> ACTIVE（保留期内反悔，卷未动）
RETENTION --到期 purge--> (不存在)    （删容器 + 删卷 + 摘 registry 条目；审计留档不可逆）
```

- `provision` 幂等：已 ACTIVE 重入为 no-op 返回现状；PAUSED / RETENTION 重入给明确指引错误
- `status`：`not_found` / `active` / `paused` / `retention`（含 `days_remaining` 剩余天数）
- 全部变更操作（含失败）追加 JSONL 审计：`<home>/audit/audit.log`，字段 `ts / action / customer_id / result(ok|error) / detail`
- 升级（滚动替换镜像）沿既有形态：deprovision 之外后续以镜像重指 + recreate 增量落地，不在 5.1 范围

## 包结构

```
cell-manager/
├── pyproject.toml            # 零运行时依赖（stdlib only）；docker 为可选 extra
├── cell_manager/
│   ├── __init__.py           # 公共 API + 与 CLI 同名的库函数
│   ├── __main__.py           # CLI（python -m cell_manager ...）
│   ├── config.py             # env 配置（全部 CELL_* 可覆盖）
│   ├── manager.py            # CellManager 状态机
│   ├── state.py              # cells.json 簿记（原子写）
│   ├── audit.py              # JSONL 审计日志
│   ├── db.py                 # 空库 + WAL 模式初始化（平台侧唯一触碰点）
│   ├── drivers/              # ContainerDriver 协议
│   │   ├── base.py           #   协议 + ContainerSpec/Info/BindMount
│   │   ├── docker.py         #   DockerDriver（docker SDK 惰性导入）
│   │   └── fake.py           #   FakeDriver（内存态，可落盘，测试/演示）
│   └── registry/             # RegistryClient 协议
│       ├── base.py           #   协议 + ServerEntry
│       ├── http.py           #   HttpRegistryClient（urllib，真实 fork API，任务 5.2 已对齐）
│       └── fake.py           #   FakeRegistryClient
└── tests/                    # 全 Fake，不打真 docker / registry
```

## 驱动抽象

- **ContainerDriver 协议**（`drivers/base.py`）：`create(spec) / start(name) / stop(name) / remove(name, force) / exists(name) / inspect(name)`
  - `DockerDriver`：docker SDK **惰性导入**——未装 SDK 时 `import` 本包不炸、实例化才报 `DriverError`（带安装指引）；daemon 不可达同样在实例化期包装报错
  - `FakeDriver`：内存容器表（create 重名报错 / start-stop 幂等 / remove 运行中须 force / inspect 暴露 env+mounts 供隔离断言）；`persist_path` 可选落盘，CLI 跨进程演示用
- **RegistryClient 协议**（`registry/base.py`）：`register(entry)` / `unregister(path) -> bool`（不存在宽松返回 False）
  - `HttpRegistryClient`：stdlib urllib，**已对齐 mcp-gateway-registry fork 真实控制台 API**（详见下节）
  - `FakeRegistryClient`：内存条目表 + 调用记录
- 工厂：`build_driver(config)` / `build_registry_client(config)` 按 `CELL_DRIVER` / `CELL_REGISTRY_MODE` 选择形态

## registry 真实 API（任务 5.2 摸底，源码 /Users/chengsishi/code/mcp-gateway-registry）

部署：cheap-1 docker compose（/opt/mcp-gateway-registry），基址 `https://mcp.finddatatech.cloud`（v1.30.0，已连通验证）。

| 操作 | 端点 | 请求体 | 成功 | 备注（源码行号） |
| --- | --- | --- | --- | --- |
| 注册 | `POST /api/servers/register` | **form 表单**：`name`/`description`/`path` 必填，remote 需 `proxy_pass_url`；`overwrite=true` | 201 | server_routes.py:4272；overwrite 缺省即 true（:4283）→ 同 path 重复注册 = 干净覆写 = 幂等；path 无 `/` 前缀服务端补齐（:4429）；403 = 无 `register_service` 权限（:4403）或覆写他人条目（:4672）；409 仅在 overwrite=false 撞已存在（:4699） |
| 摘除 | `POST /api/servers/remove` | form 表单 `path` | 200 | server_routes.py:5624；404 = 本就不存在（:5673）→ `unregister` 宽松返回 False 的语义来源；删除需 owner 或 admin（:5703-5725） |
| 探测 | `GET /api/servers` | query `limit`/`include_tools` | 200 `{"servers": [...], "total_count": N}` | server_routes.py:883；只读连通性/联调探测（`client.list_servers()`） |

**鉴权（可插拔，v1 两种形态；请求体都是 form 而非 JSON——fork 的 Form() 参数）**：

- **bearer**（推荐部署形态，cell-manager 自动化正解）：`Authorization: Bearer <凭据>`。凭据首选 **patch key**（`wgk-` 前缀，registry 定版起默认开启）：**永不过期**、MCP 入口与 registry `/api/` 双面可用、SHA256 落库吊销即时——一次铸币（控制台 UI 或 `POST /api/patch-keys`）长期无人值守使用。JWT 亦可（`POST /api/tokens/generate` 铸币；默认 8h、上限 7d，**到期需人工重铸**，不推荐常驻自动化）。链路：nginx 对 `/api/` 做 `auth_request /validate`（docker/nginx_rev_proxy_http_only.conf:415-470）→ auth_server `/validate` 校验（auth_server/server.py:3756）→ 铸 `X-Internal-Token-Registry` 转发。无会话 cookie 时 **CSRF 免检**（registry/auth/csrf.py:114-126）。
- **console-session**：`Cookie: mcp_gateway_session=...`（cookie 名 registry/core/config.py:209）+ `X-CSRF-Token` 头（有会话 cookie 时 CSRF 必检，csrf.py:136；token 用 `GET /api/auth/csrf-token` 铸币，registry/auth/routes.py:487）。registry **无用户名密码 REST 登录**（登录只走 OAuth2 浏览器流，auth_server/oauth2_providers.yml），v1 会话由 env 直接喂 cookie+CSRF。
- patch-key 通道（registry/api/patch_key_routes.py）在 registry 定版后**已上线**（`PATCH_KEY_AUTH_ENABLED` 默认 true、铸出前惰性）：它是本客户端 bearer 模式的首选凭据形态（见上）。
- 权限前提：操作账号需 `register_service` 权限（注册）与 owner/admin（删除/覆写他人）。

## 使用

### CLI

```bash
python -m cell_manager provision  <customer_id>   # 开通（幂等；条目自带组受限可见性）
python -m cell_manager pause      <customer_id>   # 暂停（数据保留）
python -m cell_manager resume     <customer_id>   # 恢复
python -m cell_manager deprovision <customer_id>  # 退租编排：吊key→删scope→注条目→保留期（幂等可重跑）
python -m cell_manager cancel-deprovision <customer_id>  # 保留期内恢复（授权面需重跑 grant）
python -m cell_manager status     <customer_id>   # 状态查询
python -m cell_manager purge-expired              # 清除到期 retention cell（定时任务）

# cell 授权束（add-customer-onboarding-automation；需 CELL_CUSTOMER_IDENTITY_MAP）
python -m cell_manager grant     <customer_id>   # 铸授权束 registry 半边：scope+条目可见性+预检映射（幂等）
python -m cell_manager mint-key  <customer_id>   # 铸客户 key（明文仅本次 stdout，立即转存/交付，勿入日志 git）
python -m cell_manager revoke-key <customer_id> <key_id>  # 吊一把 key（owner 通道，404/409 幂等成功）
```

输出一律 JSON；成功 exit 0，失败 exit 1 且 `{"ok": false, "error": {"type", "message"}}`。

### 库

```python
from cell_manager import provision, pause, resume, deprovision, cancel_deprovision, status, purge_expired
# 或注入式：
from cell_manager import CellManager, CellManagerConfig, FakeDriver, FakeRegistryClient
manager = CellManager(driver=..., registry=..., config=CellManagerConfig.from_env(), clock=...)
```

### 本机无 docker 的全生命周期演示

```bash
export CELL_DRIVER=fake CELL_REGISTRY_MODE=fake CELL_MANAGER_HOME=/tmp/cell-demo
python -m cell_manager provision acme && python -m cell_manager status acme
```

## 配置（env）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `CELL_MANAGER_HOME` | `~/.fd-wire/cell-manager` | 管理面根（卷/簿记/审计都在其下） |
| `CELL_DATA_ROOT` | `<home>/volumes` | 数据卷根；每 cell 一个 `<data_root>/<customer_id>/daas.db` |
| `CELL_STATE_FILE` | `<home>/state/cells.json` | 生命周期簿记（原子写） |
| `CELL_AUDIT_LOG` | `<home>/audit/audit.log` | JSONL 审计留档 |
| `CELL_IMAGE` | 占位镜像名 | cell 容器镜像（部署时指 TCR 真实镜像） |
| `CELL_GATEWAY_URL` | wire 占位 | 注入容器 `WIRE_GATEWAY_URL`（同门计量入口） |
| `CELL_REGISTRY_URL` | registry 占位 | registry 借用契约地址 |
| `CELL_REGISTRY_MODE` | `http` | `http` \| `fake` |
| `CELL_REGISTRY_AUTH` | 自动嗅探 | `bearer` \| `console-session` \| `none`（显式指定鉴权形态） |
| `CELL_REGISTRY_TOKEN` | - | Bearer 凭据（首选 patch key `wgk-`，永不过期；JWT 亦可；有值即选 bearer） |
| `CELL_REGISTRY_COOKIE` / `CELL_REGISTRY_CSRF` | - | 控制台会话 `mcp_gateway_session=<v>` + CSRF token（两者齐备即选 console-session） |
| `CELL_REGISTRY_REGISTER_PATH` / `CELL_REGISTRY_REMOVE_PATH` | `/api/servers/register` / `/api/servers/remove` | 端点微调（契约可重指纪律） |
| `CELL_REGISTRY_SERVERS_PATH` | `/api/servers` | 只读探测端点（list_servers） |
| `CELL_REGISTRY_TIMEOUT` | `15` | HTTP 超时秒数 |
| `CELL_TARGET_URL_TEMPLATE` | `http://{container_name}:8000/mcp` | registry 条目 target 模板 |
| `CELL_READINESS_TIMEOUT` | `120` | 就绪等待秒数（start→等健康→register；注册即触发扫描探针，未就绪会被自动禁用） |
| `CELL_RETENTION_DAYS` | `30` | 注销保留期（0=立即到期） |
| `CELL_DRIVER` | `docker` | `docker` \| `fake` |
| `CELL_CUSTOMER_CREDENTIAL_MAP` | -（空） | 客户轨凭据 JSON（`customer_id → key`），provision 时注入容器 `WIRE_CUSTOMER_KEY` |
| `CELL_PLATFORM_CREDENTIAL` | -（空） | 平台轨凭据单值（全部 cell 共用），provision 时注入容器 `WIRE_PLATFORM_KEY` |
| `CELL_CUSTOMER_IDENTITY_MAP` | -（空） | 开户身份 JSON（`customer_id → {username, sub2api_user_id}`），cell 授权束（grant/mint-key/退租）的输入 |
| `CELL_REGISTRY_SECRET_KEY_FILE` | -（空） | registry 根信任材料文件（mint-key 用）：registry `.env`（摘 `SECRET_KEY=` 行）或裸 secret（0600）；仅 cheap-1 本机配置 |
| `CELL_PREFLIGHT_MAP_PATH` | -（空） | registry 预检映射写入 API 路径（切片二任务 3.1 交付后配置）；未配置时 grant 显式跳过该步并审计留痕 |
| `CELL_SCOPE_IMPORT_PATH` / `CELL_SCOPE_DELETE_PATH` | `/api/servers/groups/import` / `/api/iam/groups` | 授权 scope 端点微调（契约可重指纪律） |

容器 env 隔离注入：`DAAS_DATABASE_URL=sqlite:////data/daas.db`（指向本 cell 卷挂载）+ `WIRE_GATEWAY_URL`（wire 入口）+ 凭据双轨（`WIRE_CUSTOMER_KEY` / `WIRE_PLATFORM_KEY`，来自上表两项）。卷以 bind mount 挂到容器 `/data`。

> **凭据双轨（生产必须配置）**：`CELL_CUSTOMER_CREDENTIAL_MAP` 与 `CELL_PLATFORM_CREDENTIAL` 未配置时 provision **不注入**这两个 env——cell 内 `wire_auth_proxy` 对缺 key 的轨道 fail-closed（503 拒绝）。生产必须两轨都配，凭据的发放/轮换约定见 [`docs/service-credentials.md`](../docs/service-credentials.md)。

## 隔离保证

- 每 cell 独立卷目录（`<data_root>/<customer_id>/`）、独立容器名与 registry 条目路径（`fd-wire-cell-<customer_id>`），互不重叠
- `customer_id` 白名单校验（`[A-Za-z0-9][A-Za-z0-9._-]{0,63}`，首字符字母数字）——杜绝路径逃逸
- 平台侧唯一触碰卷的代码是 `db.py:init_volume`（建空库+开 WAL），不读写业务数据

## DockerDriver 部署注意（给上线时）

1. **SDK**：生产镜像装 `pip install '.[docker]'`；缺 SDK 只在实例化报错（本机开发/CI 用 `CELL_DRIVER=fake`）
2. **daemon 访问**：cell-manager 容器需挂载 `/var/run/docker.sock`（或 `DOCKER_HOST` 指远程 daemon）；注意 docker.sock 等于宿主 root 权限——建议 sidecar proxy（如 tecnativa/docker-socket-proxy）只放行 containers API
3. **bind mount 路径是 daemon 侧路径**：`CELL_DATA_ROOT` 必须在 daemon 可见的文件系统上；cell-manager 与 cell 卷放同一共享卷面（compose/k8s local-path 同款约束）
4. **镜像**：`CELL_IMAGE` 默认是占位——部署时指 TCR 里 fd-daas-mcp 真实镜像（任务 1.2 发版产物）
5. **registry 鉴权凭据**：`HttpRegistryClient` 已对齐真实 fork API（见上节）。部署前需：① 用有 `register_service` 权限的账号在控制台登录，`POST /api/tokens/generate` 铸一枚 Bearer JWT（上限 7d，到期需重铸——建议运维日历提醒或后续接 patch-key 通道部署后切换）；② `export CELL_REGISTRY_TOKEN=<jwt>`（或会话形态 `CELL_REGISTRY_COOKIE` + `CELL_REGISTRY_CSRF`）。写操作账号还需是 cell 条目的 owner（同一账号注册/摘除即天然满足）
6. **purge 调度**：`purge-expired` 需外部定时触发（cron/k8s CronJob 建议 1 次/天）；到期判定以簿记 `purge_at` 为准，`CELL_RETENTION_DAYS` 只在 deprovision 时刻生效
7. **单写者假设**：簿记 cells.json 无跨进程锁——同一 home 只允许一个 cell-manager 实例操作（k8s 单副本 Deployment + `strategy: Recreate`）

## 测试

```bash
cd cell-manager && python3 -m pytest
```

覆盖：provision 幂等 / 两企业 cell 并存隔离（卷、容器名、FakeDriver mounts 互不可见）/ pause 数据保留 / deprovision→retention 剩余天数 / 到期 purge（卷删+registry 摘除+审计留档）/ 保留期内 cancel 恢复 / 审计追加（含失败留痕）/ 未知客户 status / DockerDriver 惰性导入纪律 / CLI 子进程全生命周期 / **HttpRegistryClient 真实契约**（mock urllib：注册 form 载荷与幂等覆写、摘除 200/404、401/403/409/500、不可达/超时、bearer 与 console-session 头、auth_from_env、工厂接线）。除 HttpRegistryClient 契约测试为 mock 外全 Fake，零 docker、零网络。

## 边界（后续任务）

- 任务 5.2 已对齐真实 API（端点/鉴权/字段，见上节）；剩余：用有效凭据做一次注册/摘除全链路真写联调（当前仅完成只读探测与鉴权链路验证——本地 token 均已过期，需控制台重铸）、wire 入口可达性验证
- 任务 5.3：cell 内跑 fd-daas-mcp（161 工具），`DAAS_DATABASE_URL` 消费方验证
- 存储演进：`DAAS_DATABASE_URL` 重指专属 PG database（先 spike 后迁移，ADR-0002）
