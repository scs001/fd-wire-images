#!/usr/bin/env bash
# fd-wire-api 的出片冒烟闸（出片池约定：repo 自带本脚本时优先于池的 SMOKE_MODE）。
#
# 池按 `bash scripts/ci-smoke.sh <registry>/<ns>/<img>:<tag>` 调用，工作目录是
# 快照根。
#
# 2026-11-15 的实况：池的通用 boot 探针在本镜像上连红两次，容器 4 秒即退，
# `Push image` 被 skip ⇒ 一个镜像都没出。根因是镜像自身——`WORKDIR /app` 属 root、
# 进程以 appuser(10001) 跑，而包里有 **8 个 CWD 相对的 sqlite 默认值**，任一模块
# 启动期 ensure_schema 就 `unable to open database file`。那条已在 `api/Dockerfile`
# 里从根上修掉（`RUN chown appuser:appuser /app`），所以本脚本不再是为了绕开它。
#
# 本脚本存在的理由是池看不见的那件事：**这个镜像里到底有没有「重叠窗满了会翻页」
# 那条修法**。2026-11-15 的计量停摆（水位停 11-08、64,176 条事件未入表）在生产上
# 只表现为 `fetched=500/accepted=0`，光看这个数分不出「跑的是每轮一页」还是
# 「每轮二十页但库读不出来」——`poll.poll_max_pages` 是唯一能分辨的口径，而池的
# 通用 boot 探针只问「容器还活着吗」。
#
# 判据（任一不成立即失败，失败必打容器日志）：
#   A `/health` 在 60s 内 200；
#   B `/internal/metering/channel-status` 带令牌 200（计量子系统真挂上了，
#     而不是「app 起来了但计量路由没注册」）；
#   C 同一响应里 `poll.poll_max_pages >= 2`（老镜像没这个字段或等于 1 ⇒ 红）。
#
# 刻意不做的事：不 publish 端口（避开宿主端口冲突，全程 docker exec）；不设
# REGISTRY_MONGO_URI、且 `METERING_DRIVER_ENABLED=false`（冒烟不该出网、也不该
# 在别人的库上留痕迹）。

set -euo pipefail

IMG="${1:-}"
if [ -z "$IMG" ]; then
  echo "usage: $0 <image-ref>" >&2
  exit 2
fi

# web 镜像是 nginx 静态壳（监听 8080，没有 /health），本脚本其余部分全是 api 探针。
# 按镜像名分派，否则 web 必然被 :8000/health 误判（2026-11-16 实况：fd-wire/web
# 连红三轮，每次都是端口错，镜像本身早已正常）。
case "$IMG" in
  */web:*|*/web)
    echo "smoke: image=$IMG (web shell → http://127.0.0.1:8080/)"
    WNAME="ci-smoke-web-$$"
    trap 'docker rm -f "$WNAME" >/dev/null 2>&1 || true' EXIT
    docker rm -f "$WNAME" >/dev/null 2>&1 || true
    if ! docker run -d --name "$WNAME" "$IMG" >/dev/null; then
      echo "SMOKE FAILED - web container refused to start"
      docker logs "$WNAME" 2>&1 | tail -40
      exit 1
    fi
    wok=0
    for _ in $(seq 1 12); do
      sleep 5
      # nginx:alpine 带 busybox wget：探首页，要求 200 且是 SPA 壳
      if docker exec "$WNAME" wget -q -O - http://127.0.0.1:8080/ 2>/dev/null | grep -q 'id="root"'; then
        wok=1
        echo "smoke: web shell serves index.html"
        break
      fi
      if [ "$(docker inspect -f '{{.State.Running}}' "$WNAME" 2>/dev/null)" != "true" ]; then
        echo "smoke: web container exited"
        break
      fi
    done
    if [ "$wok" != "1" ]; then
      echo "SMOKE FAILED - web shell did not serve index.html"
      docker logs "$WNAME" 2>&1 | tail -40
      exit 1
    fi
    exit 0
    ;;
esac

NAME="ci-smoke-$$"
TOKEN="smoke-$(date +%s)-$$"
BOOT_WAIT="${CI_SMOKE_BOOT_WAIT:-60}"

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "smoke: image=$IMG (probe http://127.0.0.1:8000/health)"
docker rm -f "$NAME" >/dev/null 2>&1 || true

# 刻意**不**给 DATABASE_URL 覆盖：本闸要验的就是「镜像用默认配置能不能起来」
# （`Workdir /app` 归 appuser 那条修法）。给了覆盖等于把缺陷重新藏起来。
if ! docker run -d --name "$NAME" \
      -e "METERING_INGEST_TOKEN=$TOKEN" \
      -e "METERING_DRIVER_ENABLED=false" \
      "$IMG" >/dev/null; then
  echo "SMOKE FAILED - container refused to start"
  docker logs "$NAME" 2>&1 | tail -40 || true
  exit 1
fi

probe() {  # probe <url> <bearer-or-empty>：容器内取状态码（基底无 curl，用 stdlib）
  docker exec "$NAME" python -c '
import sys, urllib.request, urllib.error
url, tok = sys.argv[1], sys.argv[2]
req = urllib.request.Request(url)
if tok:
    req.add_header("Authorization", "Bearer " + tok)
try:
    print(urllib.request.urlopen(req, timeout=3).status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception as e:
    print("ERR:%s" % e)
' "$1" "${2:-}" 2>/dev/null || echo "ERR:exec"
}

# A. /health 就绪
ok=0
for i in $(seq 1 $((BOOT_WAIT / 2))); do
  code="$(probe http://127.0.0.1:8000/health "")"
  if [ "$code" = "200" ]; then ok=1; echo "smoke: /health 200 after ~$((i*2))s"; break; fi
  if [ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" != "true" ]; then
    echo "smoke: container exited after ~$((i*2))s (code=$code)"
    break
  fi
  sleep 2
done
if [ "$ok" != "1" ]; then
  echo "SMOKE FAILED - /health never returned 200 within ${BOOT_WAIT}s"
  docker logs "$NAME" 2>&1 | tail -40 || true
  exit 1
fi

# B. 计量通道状态端点带令牌可读
body="$(docker exec "$NAME" python -c '
import sys, json, urllib.request, urllib.error
req = urllib.request.Request(sys.argv[1], headers={"Authorization": "Bearer " + sys.argv[2]})
try:
    print(urllib.request.urlopen(req, timeout=3).read().decode())
except urllib.error.HTTPError as e:
    print("HTTPERR:%d:%s" % (e.code, e.read().decode()[:200]))
except Exception as e:
    print("ERR:%s" % e)
' http://127.0.0.1:8000/internal/metering/channel-status "$TOKEN" 2>/dev/null || echo "ERR:exec")"

case "$body" in
  HTTPERR:*|ERR:*|"")
    echo "SMOKE FAILED - channel-status 不可读: ${body:0:200}"
    docker logs "$NAME" 2>&1 | tail -40 || true
    exit 1
    ;;
esac

# C. 分页上限 —— 「这个镜像里有没有 11-15 那条修法」的唯一可观测口径
pages="$(printf '%s' "$body" | python -c '
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get("poll", {}).get("poll_max_pages", "absent"))
except Exception as e:
    print("parse-error:%s" % e)
')"
echo "smoke: channel-status ok, poll.poll_max_pages=$pages"
if [ "$pages" = "absent" ]; then
  echo "SMOKE FAILED - channel-status 没有 poll.poll_max_pages：镜像里没有 2026-11-15 的分页修法（老片）"
  exit 1
fi
if [ "$pages" -lt 2 ] 2>/dev/null; then
  echo "SMOKE FAILED - poll_max_pages=$pages <2：每轮只取一页，重叠窗一满就永久停滞"
  exit 1
fi

echo "smoke: PASS (health=200, metering mounted, pagination fix present)"
