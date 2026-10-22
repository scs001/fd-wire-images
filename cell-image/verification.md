# 任务 5.3 本地验证记录（无 docker，macOS / python 3.12.13 / uv 0.11.24）

日期：2026-10-05。工作目录 `/tmp/cell-smoke-53`（一次性冒烟环境）。
网络注记：直连 PyPI 间歇 TLS 失败，依赖经清华镜像安装
（`--default-index https://pypi.tuna.tsinghua.edu.cn/simple`）——与镜像内容无关，
镜像内依赖清单与 Dockerfile 一致。

## 1. 静态检查

```
$ bash -n cell-image/entrypoint.sh          → entrypoint.sh syntax OK
$ python3 -m py_compile cell-image/wire_auth_proxy.py cell-image/healthcheck.py
                                            → py_compile OK
```

## 2. 临时 venv 安装 DAAS 包源（引擎依赖面验证）

```
$ uv venv /tmp/cell-smoke-53/venv --python 3.12
$ uv pip install "fastmcp>=3.4.2" "click>=8.0" "pandas>=1.0" "sqlalchemy>=2.0" \
    "python-dotenv>=1.0" "apscheduler>=3.11.2" "pyyaml>=6.0" "jsonschema>=4.0" \
    "pypdf>=4.0" "httpx>=0.27"          # = Dockerfile 同款白名单
$ uv pip install --no-deps -e /path/to/DAAS/fd-daas-mcp -e /path/to/DAAS/fd-daas-mcp/models
  → Installed 2 packages: fd-daas-mcp==0.1.0, mcp-models==0.1.0
$ python -c "import daas.fd_daas_mcp.registry as r; print(r.FD_HOME)"
  → /Users/chengsishi/finddata/DAAS/fd-daas-mcp      # 布局解析正确
```

结论：引擎可按「依赖白名单 + 两本地包 --no-deps」离线组装，`fd-open-data-mcp`
缺席不影响安装与启动（optional 面按缺席跳过）。

## 3. 假 wire 计量门（模拟 registry：只认 header 凭据）

`fake_gate.py`（127.0.0.1:19990）：`Authorization: Bearer` 匹配
`test-customer-key`/`test-platform-key` 放行并逐请求落日志，否则 401；最小
JSON-RPC（initialize / tools/list / tools/call）。

```
无凭据 POST → HTTP 401
带 Bearer test-customer-key POST tools/list → HTTP 200
```

## 4. 真 entrypoint.sh 拉起 cell（env 覆盖跑容器语义）

```
$ env -i PATH=…venv/bin… HOME=… \
    FD_HOME=/path/to/DAAS/fd-daas-mcp  DATA_DIR=/tmp/cell-smoke-53/data \
    MCP_PORT=18311 WIRE_PROXY_PORT=18312 \
    WIRE_UPSTREAM_URL=http://127.0.0.1:19990/fd-find-data-business-mcp/mcp \
    WIRE_CUSTOMER_KEY=test-customer-key WIRE_PLATFORM_KEY=test-platform-key \
    sh cell-image/entrypoint.sh
[entrypoint] wire_auth_proxy up at http://127.0.0.1:18312/mcp -> http://127.0.0.1:19990/…
[entrypoint] gateway upstream 'fd-open-data-mcp' -> http://127.0.0.1:18312/mcp (http, metered)
[entrypoint] exec fd-daas-mcp server: transport=http host=0.0.0.0 port=18311
             db=sqlite:////tmp/cell-smoke-53/data/daas.db     ← 四斜杠绝对路径正确
fd-daas-mcp server: registered=161 failed=0 skipped_optional=1  ← 161 工具全量注册
                                                              （skipped=pdf 组，符合设计）
```

引擎进程 env 抽查：`NO_PROXY=127.0.0.1,localhost,::1` 已由 entrypoint 注入。

## 5. MCP 面冒烟（fastmcp Client 连 http://127.0.0.1:18311/mcp）

```
initialize ok: fd-daas-mcp | protocol: 2026-07-28
tools/list count = 161
gateway_get_data_mcp → {"name": "fd-open-data-mcp", "transport": "http",
  "url": "http://127.0.0.1:18312/mcp", "enabled": true, "command": null,
  "description": "wire metering gate (registry business-mcp) via in-cell auth proxy [wire-platform-v1 5.3]", …}
gateway_list_data_mcp_tools → {"server": "fd-open-data-mcp", "count": 1,
  "tools": [{"name": "fake_read", "description": "fake metered read"}]}
CHAIN OK: engine -> gateway pool -> auth proxy -> fake wire gate
```

curl 直打引擎 initialize（healthcheck 同款请求）→ `HTTP 200` +
`{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18",…}}`。

## 6. 凭据注入断言（假门日志，逐请求）

```
{"path": "/fd-find-data-business-mcp/mcp", "auth": "Bearer test-customer-key", "track": "customer"}  ×4
```

引擎经网关发出的每一个 MCP 请求都带注入的客户凭据 → registry 侧按凭据
归属计量/结算，链路成立。

## 7. 双轨路由 / fail-closed / 无门清理 / 健康检查

```
POST 127.0.0.1:18312/mcp-platform tools/list → 200，假门日志：
  {"auth": "Bearer test-platform-key", "track": "platform"}     ← 平台轨独立凭据
POST 127.0.0.1:18312/nope → 404（未知路由）
无 key 代理实例：/mcp 与 /mcp-platform 均
  → 503 {"error": "credential for track '…' not configured (fail-closed)"}
无 WIRE_GATEWAY_URL 的 entrypoint 运行 → 预置的陈旧 upstream 行被删除
  （get_upstream 返回 None，读取回落本地、不指向陈旧端点）
$ env MCP_TRANSPORT=http MCP_PORT=18311 python cell-image/healthcheck.py
  → healthcheck: initialize ok (HTTP 200)，exit 0
```

## 8. 过程中踩到的两个坑（已固化防御，供后人）

1. **测试脚本命名 `bisect.py` 遮蔽 stdlib**：`python -m` 把 cwd 放进
   sys.path，引擎侧（pandas 等）`import bisect` 会执行我的测试脚本（import 期
   跑 registry.build()），症状是莫名 `Client failed to connect: [Errno 8]`。
   教训：冒烟脚本别叫 stdlib 名字（已改名 group_bisect.py）。
2. **机器本地 `DAAS/.env` 含 `HTTP_PROXY=http://test-proxy:8081`（占位）**：
   alerts/dashboard 组启动时 `load_dotenv(DAAS/.env)` 把它带进引擎进程，
   httpx trust_env 把引擎→127.0.0.1:8312 的回环连接路由去 `test-proxy`
   → DNS 解析失败即 Errno 8。该文件是机器本地工件，**镜像不 COPY 任何 .env**
   （Dockerfile 构建期 `find … -name ".env" -delete` 兜底）；entrypoint 另注入
   `NO_PROXY=127.0.0.1,localhost,::1` 双保险。

## 9. 引擎零改动验证

`git -C DAAS/fd-daas-mcp status --porcelain` 共 16 条，全部为**本任务开始前
已存在**的未提交改动（Dockerfile、alerts-mcp/notifiers 清理、daas-mcp 旧脚本
删除、server.py、k8s 清单等——本任务只读未写；editable 安装刷新的
`fd_daas_mcp.egg-info/` 为 gitignored）。清单快照：

```
 M Dockerfile                    M alerts-mcp/selfcheck.py
 M alerts-mcp/notifiers/__init__.py       D daas-mcp/fetch_data_cli.py
 D alerts-mcp/notifiers/dingtalk.py …（6 个 notifier 删除）
 M alerts-mcp/notifiers/registry.py       D daas-mcp/migrate_entity_collection_keys.py
 M daas-mcp/pyproject.toml                D daas-mcp/migrate_process_cron.py
 M daas/fd_daas_mcp/server.py             M k8s/zihan/50-fd-daas-mcp.yaml
```

## 10. 未做/受限项（如实）

- **docker build 未实际执行**（本机无 docker）：Dockerfile 验证 = 结构审查 +
  构建期布局/安装自检语句 + 上述同依赖同入口逻辑的本地全链冒烟。
  named-context COPY 语义（`COPY --from=daas <dir>`）为 BuildKit 文档行为，
  首次 CI 构建如遇出入以 CI 日志修正。
- 对**真实** registry wire 入口的联调（真证书/真 key/真 Caddy 改写）不在本地
  冒烟范围；`WIRE_SERVER_PATH` / `WIRE_UPSTREAM_URL` 即为联调期的对齐旋钮。
