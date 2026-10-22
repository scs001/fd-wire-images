# cell-image — DAAS 引擎 cell 运行镜像（wire-platform-v1 任务 5.3）

把 DAAS 引擎（fd-daas-mcp，161 工具 9 组 MCP）打包成 wire 企业 cell 的运行镜像：
**引擎零改动**，数据读取委托（gateway 的 `fd-open-data-mcp` upstream）在容器启动时
重指到 **wire 计量门**（registry wire 入口的 business-mcp 条目），凭据由容器 env 注入，
计量归属由凭据轨道决定。

```
四件套：
  Dockerfile         镜像构建（BuildKit named context `daas`）
  entrypoint.sh      env 前置注入 + 凭据代理拉起 + gateway upstream 重指 + exec 引擎
  wire_auth_proxy.py cell 内回环凭据代理（Authorization 注入；双凭据轨道）
  healthcheck.py     MCP initialize 握手探活（镜像 HEALTHCHECK 用）
  verification.md    本地无 docker 冒烟记录（全链绿）
```

## 架构（cell 内部）

```
                ┌────────────────────────── cell 容器（本镜像）──────────────────────────┐
                │                                                                          │
 物化任务        │  fd-daas-mcp 引擎（161 工具，http :8000/mcp）                            │
 （刷新/计算/    │    │ 数据读取委托                                                          │
  报告出数）     │    ▼ gateway_upstreams['fd-open-data-mcp'] = http://127.0.0.1:8312/mcp   │
                │  wire_auth_proxy（回环 :8312，Authorization: Bearer 注入）                  │
                │    ├─ /mcp          → WIRE_CUSTOMER_KEY（客户轨，计费）                     │
                │    └─ /mcp-platform → WIRE_PLATFORM_KEY（平台轨，归集不结算）               │
                └────────────────────────────────┬─────────────────────────────────────────┘
                                                 │ https（带 per-user 长命 key）
                                                 ▼
                 wire 计量门 = registry wire 入口（mcp.finddatatech.cloud）
                               → /mcp-proxy/business-mcp/… → business-mcp（rows_returned 计量戳）
                               → 每调用一条 MCPServerAccessRecord 审计 → 4.x 计量/结算管道
```

## 摸底结论：为什么要垫一个回环凭据代理（关键设计依据）

1. **DAAS 网关的委托面**：`daas-mcp/registry_service.py` 的数据读取按名字
   `fd-open-data-mcp` 走 `gateway-mcp/gateway_tools.py → call_data_mcp_sync`；
   端点配置不在代码里，而在 **`gateway_upstreams` 表**（落在
   `DAAS_DATABASE_URL` 指向的 daas.db——cell 里就是 /data 卷，天然每 cell 独立）。
   http 形态建客户端只用 URL：
   `Client(StreamableHttpTransport(url))`（`gateway_client_pool/client_pool.py`
   `_build_client_from_row`、`gateway_database.py` `build_client`）——**没有 header 面**。
2. **计量门只认 header**：registry fork（mcp-gateway-registry）鉴权走
   `Authorization: Bearer <per-user 长命 key>` / `X-Api-Key` 头（auth_server
   `/validate` 子请求），**不支持 URL query 传 key**。
3. 两个事实相撞 → 引擎自己发不出带凭据的请求。引擎不许改（与謙面本地产品
   零 diff），所以在 cell 内垫 `wire_auth_proxy`：引擎打回环 127.0.0.1:8312
   （无凭据），代理补 `Authorization` 再转发 wire 门。凭据只存在于容器 env
   （平台面），不存在于引擎进程配置——引擎代码里没有任何 tenant/凭据概念。

**引擎原生替代方案（未采用，交主代理决策）**：给 DAAS gateway 加
`headers_json` 列并在两处 `build_client` 传 `headers=`（fastmcp
`StreamableHttpTransport` 本身支持 headers 参数）。约 20 行 + 一列 schema，
可去掉代理这一层；代价是 DAAS 仓不再是零改动，与「引擎不多用户化」边界
冲突，故本任务以代理方案落地。

## 双凭据边界（如实声明）

- **客户轨（默认）**：`gateway_upstreams` 的 `fd-open-data-mcp` 行指
  `http://127.0.0.1:8312/mcp` → 代理注入 `WIRE_CUSTOMER_KEY`。cell 内一切经
  DAAS 网关的数据读取（物化任务、引擎工具触发的 fetch）都走这条轨 → 计费到客户。
- **平台轨（可用但未被引擎默认使用）**：代理另开 `/mcp-platform` 路由注入
  `WIRE_PLATFORM_KEY`（平台服务凭据，归集不结算）。DAAS 把 upstream 名字
  `fd-open-data-mcp` 写死、一行一凭据，引擎进程内**无法**按调用切换凭据轨道——
  所以 AI 探索的平凭据轨道**不经过 DAAS 网关**：5.4 的 `CellMcpExecutor`
  直接持平台凭据调 wire 入口/cell 端点（api/fd_wire_api/ai/executor.py 现状
  即如此）。`/mcp-platform` 路由留给 5.6 调度器或未来 executor 在 cell 网络内
  直连使用，不改变引擎行为。
- 若同轨双凭据成为硬需求（同一物化流水线里混合归属），只能走上面「引擎原生
  替代方案」或 per-task 第二 upstream 行——都需要 DAAS 侧配合，超出本任务边界。

## 构建（named context，本机）

DAAS 仓私有未发 PyPI → 不能 git clone/pip 引用，用 BuildKit named context：

```sh
docker buildx build \
  --build-context daas=/Users/chengsishi/finddata/DAAS/fd-daas-mcp \
  -t ccr.ccs.tencentyun.com/fd-wire/cell:sha-$(git rev-parse --short HEAD) \
  cell-image/
```

- 镜像内树必须落在 `/opt/fd-daas-mcp`：registry（`daas/fd_daas_mcp/registry.py`）
  以 `FD_HOME = parents[3]/fd-daas-mcp` 采集 9 个工具组源码目录——**纯 pip 安装不够**
  （wheel 只含 `daas.*`），必须整树 COPY；Dockerfile 构建期有布局自检。
- `pdf-mcp` 组不进镜像（optional，dep `sqlite_vec` 缺席时引擎自动跳过）。
- 根 pyproject 的两个机器本地 path 依赖：`mcp-models` 随树 `--no-deps` 安装；
  `fd-open-data-mcp` **故意缺席**——cell 的取数一律经计量门，不留本地 stdio 拉数通道。

## env 清单

### cell-manager provision 已注入（任务 5.1 现状）

| env | 例子 | 说明 |
|---|---|---|
| `DAAS_DATABASE_URL` | `sqlite:////data/daas.db` | 每 cell 独立卷库（四斜杠=绝对路径）；gateway upstream 行也落这里 |
| `WIRE_GATEWAY_URL` | `https://wire.finddatatech.cloud/mcp` | wire 门基址（Caddy → registry wire 入口） |

### 本镜像消费（凭据/指向——**需任务 5.2 在 cell-manager 契约对齐时补注入**）

| env | 默认 | 说明 |
|---|---|---|
| `WIRE_CUSTOMER_KEY` | （无） | 客户 per-user 长命 key（registry patch-key 铸造）；缺省时代理对客户轨 fail-closed 503（读取报错、不静默旁路计量） |
| `WIRE_PLATFORM_KEY` | （无） | 平台服务凭据；缺省则 `/mcp-platform` 轨 503 |
| `WIRE_SERVER_PATH` | `fd-find-data-business-mcp/mcp` | 拼 `WIRE_GATEWAY_URL` 后的条目路径；registry 条目路径或 Caddy 改写变化时调这里 |
| `WIRE_UPSTREAM_URL` | （自动拼出） | 完整 wire 门 URL 一票覆盖（联调/测试用） |
| `WIRE_PROXY_HOST` / `WIRE_PROXY_PORT` | `127.0.0.1` / `8312` | 凭据代理监听（回环，不 EXPOSE） |
| `MCP_TRANSPORT` / `MCP_HOST` / `MCP_PORT` | `http` / `0.0.0.0` / `8000` | 引擎自身 MCP 面；`:8000` 与 cell-manager `DEFAULT_TARGET_URL_TEMPLATE` 对齐 |
| `MCP_BEARER_TOKEN` | （无） | 引擎自身 HTTP 入口鉴权（wire→cell 这一跳）；设置后 healthcheck 自动附带 |
| `DATA_DIR` | `/data` | 卷挂载点（冒烟时可重指） |

## 与 cell-manager 的接法

```sh
CELL_IMAGE=ccr.ccs.tencentyun.com/fd-wire/cell:sha-<immutable>   # 替换 placeholder 默认
CELL_GATEWAY_URL=https://wire.finddatatech.cloud/mcp             # 已有
# 任务 5.2 契约对齐时还需把 WIRE_CUSTOMER_KEY（及可选 WIRE_PLATFORM_KEY）加进
# manager.py provision 的 spec.env——凭据按 customer 归属在开通时铸造/注入
```

镜像只认 env，不感知 cell-manager；`fd-wire-cell-*` 容器名/条目路径由 manager 侧生成。

## CI 接线（`.github/workflows/build.yml` 已按此合入——daas-deployment-profiles 1.5）

matrix 加第三项 + DAAS 仓二次 checkout + `additional_contexts`：

```yaml
      matrix:
        include:
          - image: api
            context: api
          - image: web
            context: web
          - image: cell                       # ← 新增
            context: cell-image
    steps:
      - uses: actions/checkout@v4
      # cell 镜像需要 DAAS 仓源码作 named context（排在 tag 步骤前）。
      # FindDataTechnology/fd-daas-mcp 是公开仓：main=开源净化线，
      # internal-main=内部开发线（cell 引擎能力在此线），cell 必须用内部线。
      - uses: actions/checkout@v4             # ← 新增（仅 cell 需要；矩阵项可用 if 区分）
        if: matrix.image == 'cell'
        with:
          repository: FindDataTechnology/fd-daas-mcp   # 公开仓，无需 token
          path: daas-src
          ref: internal-main
      # ……tag / login 步骤不变……
      - uses: docker/build-push-action@v6
        with:
          context: ${{ matrix.context }}
          platforms: linux/amd64
          push: true
          provenance: false
          additional_contexts: |              # ← 新增（仅 cell 项生效，其余 matrix 项为空）
            daas=${{ github.workspace }}/daas-src
          tags: |
            ${{ env.REGISTRY }}/${{ env.NAMESPACE }}/${{ matrix.image }}:${{ steps.tag.outputs.tag }}
            ${{ env.REGISTRY }}/${{ env.NAMESPACE }}/${{ matrix.image }}:main
          cache-from: type=gha,scope=${{ matrix.image }}
          cache-to: type=gha,scope=${{ matrix.image }},mode=max
```

注意：`daas-src` checkout 会覆盖主 checkout 的次要路径——`path: daas-src`
独立目录无冲突；DAAS 仓 ref 建议锁 sha 以保证镜像可复现（与 k8s 清单
不可变 sha-tag 纪律一致）。

## 健康检查

镜像 `HEALTHCHECK` = `python /app/healthcheck.py`：对 `127.0.0.1:$MCP_PORT/mcp`
打 JSON-RPC `initialize`，HTTP 200 且响应形似 JSON-RPC 即健康；
`MCP_BEARER_TOKEN` 设置时自动附带；stdio 形态退化为进程存活语义。

## 本地验证（无 docker，本机实测）

见 `verification.md`：临时 venv 装 DAAS 包源 → 真 entrypoint.sh 拉起引擎
（161 工具注册）→ initialize/tools/list → 引擎网关工具全链
（引擎→网关池→凭据代理→假 wire 门）→ 门侧日志断言每请求都带
`Authorization: Bearer <注入凭据>` → 双轨路由/fail-closed/无门清理/健康检查全过。
两个实测踩坑（stdlib `bisect` 同名脚本遮蔽；机器本地 `DAAS/.env` 的
`HTTP_PROXY` 占位经引擎 `load_dotenv` 泄漏破坏回环连接）均已以
entrypoint 的 `NO_PROXY` 防御固化，细节在 verification.md。

## 边界与如实声明

- 引擎零改动：DAAS 仓 tracked 文件零 diff（verification.md 附仓内存量改动清单，
  均为本任务之前已存在的未提交改动，非本任务产物）。
- 凭据代理是 cell 内新增组件（非引擎改动）；它只做头注入转发，无业务逻辑，
  stdlib 实现零新依赖，回环监听不对外。
- 本机无 docker：镜像构建未实际执行，Dockerfile 为结构审查+依赖/布局自检
  （构建期 `python -c` 布局断言）+ 本地全链冒烟的等价验证；首次 CI 构建时
  如有 COPY 层细节出入，以 CI 日志为准修正。
