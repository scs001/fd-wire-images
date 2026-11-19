# syntax=docker/dockerfile:1
# cell 镜像：fd-daas-mcp 引擎跑进 wire 企业 cell（openspec wire-platform-v1 任务 5.3）。
#
# 构建上下文约定（DAAS 仓私有、未发 PyPI → 不能 git clone / pip 引用）：
# 用 BuildKit named context 把本机 DAAS 仓源码挂进构建：
#
#   docker buildx build \
#     --build-context daas=/path/to/finddata/DAAS/fd-daas-mcp \
#     -t ccr.ccs.tencentyun.com/fd-wire/cell:sha-xxxxxxx \
#     cell-image/
#
# CI 接线（GHA build-push-action 的 additional_contexts，先二次 checkout 私有
# DAAS 仓）见本目录 README.md「CI 接线」。
#
# 引擎零改动约束（与謙面本地产品一致性 diff 为空）决定了两个非常规做法：
#   1. 整树 COPY 而非纯 pip 安装：daas/fd_daas_mcp/registry.py 按源码布局
#      采集工具组（FD_HOME=parents[3]/fd-daas-mcp 下的 models/ 与 *-mcp/
#      同级目录），wheel 只装出 daas.* 包，不含这些目录 → 树必须原样落在
#      /opt/fd-daas-mcp（见 README「为什么是整树 COPY」）。
#   2. 依赖白名单安装：根 pyproject 声明了两个机器本地 path 依赖
#      (mcp-models、fd-open-data-mcp)。mcp-models 随树 --no-deps 安装；
#      fd-open-data-mcp 故意缺席——cell 的数据读取经 wire 计量门 http 委托
#      （entrypoint 重指 gateway upstream），绝不本地 stdio 拉数。

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # 与 bin/fd-daas-mcp-server 同款：源码树优先于 site-packages，registry
    # 的 FD_HOME 推导才能落在 /opt/fd-daas-mcp
    PYTHONPATH=/opt/fd-daas-mcp

# ---- 运行依赖（= DAAS 根 pyproject dependencies 减去两个本地 path 包） ----
# 版本下限照抄根 pyproject；fastmcp 自带 uvicorn/starlette/httpx/mcp。
# pyyaml/jsonschema/pypdf 来自 daas-mcp 组（process 工具链）的声明。
RUN pip install \
      "fastmcp>=3.4.2" \
      "click>=8.0" \
      "pandas>=1.0" \
      "pandera>=0.34" \
      "regex>=2024.0" \
      "sqlalchemy>=2.0" \
      "python-dotenv>=1.0" \
      "apscheduler>=3.11.2" \
      "pyyaml>=6.0" \
      "jsonschema>=4.0" \
      "pypdf>=4.0" \
      "httpx>=0.27" \
      "psycopg[binary]>=3.1",  # cell PG 数据根可选后端（wire-cell-postgres-storage）：DAAS_DATABASE_URL 指向 postgresql+psycopg:// 时启用；不带该 env 的 cell 仍是 sqlite 卷，行为不变

# ---- 引擎源码（named context `daas`；目录清单 = registry.SOURCES 全组 + models） ----
# pdf-mcp 为 optional 组（dep sqlite_vec 缺席即跳过），不进镜像。
COPY --from=daas daas /opt/fd-daas-mcp/daas
COPY --from=daas models /opt/fd-daas-mcp/models
COPY --from=daas alerts-mcp /opt/fd-daas-mcp/alerts-mcp
COPY --from=daas cron-mcp /opt/fd-daas-mcp/cron-mcp
COPY --from=daas composite-mcp /opt/fd-daas-mcp/composite-mcp
COPY --from=daas daas-mcp /opt/fd-daas-mcp/daas-mcp
COPY --from=daas customer-dataset-mcp /opt/fd-daas-mcp/customer-dataset-mcp
COPY --from=daas dashboard-mcp /opt/fd-daas-mcp/dashboard-mcp
COPY --from=daas gateway-mcp /opt/fd-daas-mcp/gateway-mcp
COPY --from=daas research-mcp /opt/fd-daas-mcp/research-mcp
COPY --from=daas workflow-mcp /opt/fd-daas-mcp/workflow-mcp
COPY --from=daas pyproject.toml /opt/fd-daas-mcp/pyproject.toml
# named-context COPY 不吃 .dockerignore：手动清掉可能裹进来的任何 .env
# （引擎多组启动时会 load_dotenv 仓库 .env——机器本地 env 决不进镜像）
RUN find /opt/fd-daas-mcp -maxdepth 2 -name ".env" -delete

# 本地两包 --no-deps 安装：daas.* + fd-daas-mcp CLI 入口 + models 模块
# （site-packages 侧冗余副本，兜底非 PYTHONPATH 上下文的 `import models`）。
RUN pip install --no-deps /opt/fd-daas-mcp /opt/fd-daas-mcp/models \
 # 布局自检：registry 的 FD_HOME 推导必须解析回 /opt/fd-daas-mcp
 && python -c "\
import daas.fd_daas_mcp.registry as r; \
assert r.FD_HOME.is_dir() and r.FD_HOME.name == 'fd-daas-mcp', r.FD_HOME; \
print('layout ok:', r.FD_HOME)"

# ---- cell 包装层（本目录） ----
COPY entrypoint.sh wire_auth_proxy.py healthcheck.py /app/
RUN chmod +x /app/entrypoint.sh

# cell 的 MCP 面板：cell-manager DEFAULT_TARGET_URL_TEMPLATE 指 :8000/mcp。
# 8312 为回环凭据代理（WIRE_PROXY_HOST 默认 127.0.0.1），刻意不 EXPOSE。
EXPOSE 8000

# MCP initialize 握手探活（stdio 形态自动退化为进程存活语义）。
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD ["python", "/app/healthcheck.py"]

# /data 由 cell-manager 以卷挂载（container_path=/data）；引擎库、审计、
# gateway_upstreams 行全部落在卷里，随 cell 生命周期持久。
ENTRYPOINT ["/app/entrypoint.sh"]
