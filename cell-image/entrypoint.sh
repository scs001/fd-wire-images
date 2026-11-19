#!/bin/sh
# cell 容器入口（wire-platform-v1 任务 5.3）。
#
# 职责（在 exec 引擎进程前完成全部 env 前置注入）：
#   1. 默认 env：DAAS_DATABASE_URL=sqlite:////data/daas.db（cell-manager 卷）、
#      MCP_TRANSPORT=http / MCP_HOST=0.0.0.0 / MCP_PORT=8000（cell-manager
#      target_url 模板的 :8000 对齐）。DAAS 引擎自身的 env 面零改动。
#   2. 起 wire_auth_proxy（回环 127.0.0.1:8312）：给 DAAS 网关的 http 委托
#      补 Authorization: Bearer（引擎的 StreamableHttpTransport 只有 URL 面，
#      registry 门只认 header——详见 wire_auth_proxy.py 头注）。
#   3. 把 gateway_upstreams 里写死的 "fd-open-data-mcp" 行 upsert 成
#      http://127.0.0.1:8312/mcp（经代理=wire 计量门+凭据）。写在 cell 自己
#      的 daas.db（DAAS_DATABASE_URL）里——引擎零改动、每 cell 独立。
#   4. exec fd-daas-mcp server（PID1=引擎，信号直达）。
#
# 可移植性：FD_HOME / DATA_DIR / 脚本目录均可 env 覆盖，同一份脚本可在
# macOS 上做无 docker 冒烟（见 verification.md）。
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
FD_HOME="${FD_HOME:-/opt/fd-daas-mcp}"
DATA_DIR="${DATA_DIR:-/data}"
PROXY_HOST="${WIRE_PROXY_HOST:-127.0.0.1}"
PROXY_PORT="${WIRE_PROXY_PORT:-8312}"

# ---- 1. 引擎 env 默认值（已设置的 env 原样尊重——cell-manager 注入优先） ----
# sqlite 绝对路径=四斜杠：'sqlite:///' + '/data/…'（DATA_DIR 以 / 开头）
export DAAS_DATABASE_URL="${DAAS_DATABASE_URL:-sqlite:///${DATA_DIR}/daas.db}"
mkdir -p "$DATA_DIR"
export MCP_TRANSPORT="${MCP_TRANSPORT:-http}"
export MCP_HOST="${MCP_HOST:-0.0.0.0}"
export MCP_PORT="${MCP_PORT:-8000}"

# 引擎→凭据代理是回环连接；防御任何注入的代理 env（httpx trust_env 会把
# 127.0.0.1 也路由去 proxy 导致 ENOENT 级连不上——本地冒烟时 DAAS/.env 的
# HTTP_PROXY 占位就踩过这坑）。凭据代理自身走 stdlib http.client，不受影响。
case ",${NO_PROXY:-}," in
    *",127.0.0.1,"*) ;;
    *) export NO_PROXY="${NO_PROXY:+${NO_PROXY},}127.0.0.1,localhost,::1" ;;
esac

# ---- 2. wire 门 URL 组装（完整覆盖 > 门基址+条目路径 > 未配置） ----
if [ -n "${WIRE_UPSTREAM_URL:-}" ]; then
    GATE_URL="$WIRE_UPSTREAM_URL"
elif [ -n "${WIRE_GATEWAY_URL:-}" ]; then
    # 默认条目路径 = registry 的 business-mcp 条目（<path>/<端点>；外部 /mcp-proxy/ 前缀已随 registry 定版镜像移除）
    GATE_URL="${WIRE_GATEWAY_URL%/}/${WIRE_SERVER_PATH:-fd-find-data-business-mcp/mcp}"
else
    GATE_URL=""
fi
export WIRE_UPSTREAM_URL="$GATE_URL"

if [ -z "$GATE_URL" ]; then
    echo "[entrypoint] WARN: WIRE_GATEWAY_URL not set - cell runs offline;" \
         "gateway reads will fail and fall back local (no metering)" >&2
elif [ -z "${WIRE_CUSTOMER_KEY:-}" ]; then
    echo "[entrypoint] WARN: WIRE_CUSTOMER_KEY not set - proxy is fail-closed" \
         "(503) on the customer track; gateway reads will error until a key" \
         "is injected" >&2
fi

# ---- 3a. 凭据代理（仅当配置了 wire 门） ----
PROXY_URL=""
if [ -n "$GATE_URL" ]; then
    python -u "$SCRIPT_DIR/wire_auth_proxy.py" >/dev/stdout 2>/dev/stderr &
    PROXY_URL="http://${PROXY_HOST}:${PROXY_PORT}/mcp"

    proxy_ready() {
        python - "$PROXY_HOST" "$PROXY_PORT" <<'PY'
import sys, urllib.request
urllib.request.urlopen(f"http://{sys.argv[1]}:{sys.argv[2]}/healthz", timeout=1).read()
PY
    }
    i=0
    until proxy_ready 2>/dev/null; do
        i=$((i + 1))
        [ "$i" -ge 40 ] && { echo "[entrypoint] FATAL: wire_auth_proxy did not come up" >&2; exit 1; }
        sleep 0.25
    done
    echo "[entrypoint] wire_auth_proxy up at ${PROXY_URL} -> ${GATE_URL}"
fi

# ---- 3b. gateway upstream 重指（fd-open-data-mcp → 计量门经代理） ----
# 幂等：无门配置则显式删行（cell 离线运行时不留陈旧指向）。
python - "$PROXY_URL" "$FD_HOME" <<'PY'
import sys

proxy_url, fd_home = sys.argv[1], sys.argv[2]
sys.path.insert(0, f"{fd_home}/models")
sys.path.insert(0, f"{fd_home}/gateway-mcp")

from gateway_database import get_gateway_db  # noqa: E402 (path set above)

NAME = "fd-open-data-mcp"  # registry_service 委托时写死的 upstream 名
db = get_gateway_db()  # 落在 DAAS_DATABASE_URL 指向的 cell 库
if proxy_url:
    db.upsert_upstream(
        name=NAME,
        transport="http",
        url=proxy_url,
        command=None,  # 不留 stdio 兜底：http 失败即报错，不静默换本地进程
        args=None,
        env=None,
        cwd=None,
        enabled=True,
        description="wire metering gate (registry business-mcp) via in-cell auth proxy [wire-platform-v1 5.3]",
    )
    row = db.get_upstream(NAME)
    assert row and row["transport"] == "http" and row["url"] == proxy_url, row
    print(f"[entrypoint] gateway upstream {NAME!r} -> {row['url']} (http, metered)")
else:
    db.delete_upstream(NAME)
    print(f"[entrypoint] gateway upstream {NAME!r} removed (no wire gate configured)")
PY

# ---- 4. exec 引擎（PID1；PYTHONPATH 同 bin/fd-daas-mcp-server 惯例） ----
export PYTHONPATH="$FD_HOME${PYTHONPATH:+:$PYTHONPATH}"
echo "[entrypoint] exec fd-daas-mcp server: transport=$MCP_TRANSPORT host=$MCP_HOST port=$MCP_PORT db=$DAAS_DATABASE_URL"
exec python -m daas.fd_daas_mcp.server
