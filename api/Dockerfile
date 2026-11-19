# fd-wire-api image — FastAPI 后台/结算骨架（wire-platform-v1 任务 3.4）。
#
# 形制照抄 fd-proxy-service / scraw-fd-open-data-mcp：python slim 两阶段，
# builder 建 /opt/venv，runtime 只带 venv + 包源码。构建产物由
# .github/workflows/build.yml 推 TCR：ccr.ccs.tencentyun.com/fd-wire/api:sha-<短哈希>。
#
# BASE_IMAGE 可覆盖（fd-proxy-service 同约定）：cheap-fleet 构建机够不到
# Docker Hub 时经 --build-arg 传镜像源；默认值保证 GitHub Actions 直用。
ARG BASE_IMAGE=python:3.12-slim

FROM ${BASE_IMAGE} AS builder
WORKDIR /build
# 只拷构建所需：hatchling wheel 的 packages=["fd_wire_api","fd_mcp_client"]，
# tests 不进镜像。
COPY pyproject.toml ./
COPY fd_wire_api ./fd_wire_api
COPY fd_mcp_client ./fd_mcp_client
# 本体内核走**仓内 vendored wheel**（api/vendor/，不进 PyPI）：pyproject 里声明了
# `fd-ontology-kernel>=0.1.0`，`--find-links vendor` 让它就地找到那份 .whl。
# 漏这步的后果不是"少一个特性"：entities/versioning.py 在模块顶层 import 它，
# 而 app.py 启动即 import entities ⇒ 容器在 import 期就崩，k8s 只看到
# CrashLoopBackOff——而构建成功、本地单测（自己塞了内核路径）也全绿。
COPY vendor ./vendor
# 依赖闭包全为纯/pydantic-core 轮子，无需编译工具链（fd-proxy-service 同判）。
# [mongo] extra：计量入账驱动 registry Mongo 直读的 pymongo（驱动惰性导入，
# 缺了 poll 段静默重试——add-customer-onboarding-automation 5.4 实锤后进镜像）。
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip setuptools wheel \
 && /opt/venv/bin/pip install --no-cache-dir --find-links vendor '.[mongo]'

# 起不来的镜像不算出片成功：import 一次真正的 ASGI 入口（不是 `import fd_wire_api`，
# 那个不会走到 entities 链）。构建期就红，比上线后 CrashLoop 便宜得多。
RUN /opt/venv/bin/python -c "import fd_wire_api.app as m; assert m.app is not None"

FROM ${BASE_IMAGE}
# 非 root 运行（任务要求；fd-find-data-business-mcp 的 appuser 同法）。
RUN useradd --system --uid 10001 --create-home appuser
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
# venv 即运行时；包源码随镜像以便现场核查（k8s 不挂卷，配置全走 env →
# Secret fd-wire-env，见 deploy/k8s/fd-wire-api.yaml）。
COPY --chown=appuser:appuser fd_wire_api ./fd_wire_api
COPY --chown=appuser:appuser fd_mcp_client ./fd_mcp_client
# 运维脚本随镜像（对账/自检类只读脚本；k8s 不挂卷，现场要能直接跑）
COPY --chown=appuser:appuser scripts ./scripts
# `/app` 目录本身必须归 appuser：`COPY --chown` 只改**文件**，`WORKDIR /app` 建出的
# 目录仍是 root 所有 ⇒ 非 root 进程建不了新文件，而本包有 8 个 CWD 相对的 sqlite
# 默认值（`sqlite:///./fd_wire_dev.db`、…/fd_wire_demands.db、…/fd_wire_datasets.db、
# …/fd_wire_schedules.db、…/fd_wire_packs.db、…/fd_wire_entities.db、…/fd_wire_ai.db、
# …/billing.db），任一模块在启动期 ensure_schema 就会 `sqlite3.OperationalError:
# unable to open database file`。生产连 PG 所以从没暴露——但「默认配置起不来」的
# 镜像在 DR / 新环境 / 现场 `docker run` 调试时全都表现为 CrashLoop，看起来像镜像
# 坏了（2026-11-15 出片池的通用 boot 探针连红两次就是这么来的）。
RUN chown appuser:appuser /app
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER appuser
EXPOSE 8000
# 无 curl 的 slim 基底：用 stdlib 探 /health（app.py 提供该端点）。
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"
ENTRYPOINT ["uvicorn", "fd_wire_api.app:app", "--host", "0.0.0.0", "--port", "8000"]
