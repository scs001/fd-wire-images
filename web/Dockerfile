# syntax=docker/dockerfile:1
# fd-wire-web image — Vite 构建产物由 nginx 托管（wire-platform-v1 任务 3.4）。
#
# 形制照抄 fd-wanxing wanxing-web 的 builder/runtime 两段，但目标是纯静态：
# builder 产出 dist/，runtime 只跑 nginx（非 root，8080）。
# SPA 路由（/ 与 /console 的 try_files）与 /mcp 占位说明在 nginx.conf。
ARG NODE_IMAGE=node:22-bookworm-slim
ARG NGINX_IMAGE=nginx:1.27-alpine

FROM ${NODE_IMAGE} AS builder
WORKDIR /app
# 依赖层先行，锁文件变化才失效。
COPY package.json package-lock.json ./
RUN npm ci --no-fund --no-audit
COPY tsconfig.json vite.config.ts index.html ./
COPY src ./src
# package.json build = tsc --noEmit && vite build → dist/
RUN npm run build

FROM ${NGINX_IMAGE}
# server 块模板（8080、SPA fallback、/internal 反代 + X-Admin-Token 注入）。
# envsubst 管道（wire-self-serve-launch 5.2 附带）：nginx.conf 为模板，启动时
# CMD 以白名单 '$FD_WIRE_ADMIN_TOKEN' 渲染到 conf.d/default.conf——只替换该
# 环境变量，nginx 运行时变量（$host/$uri…）原样保留；值仅存在容器 env，
# 永不进构建产物/浏览器。未设置该 env 时注入空值 → 后端 fail-closed 403。
COPY nginx.conf /etc/nginx/conf.d/default.conf.template
# 非 root：master 由 USER nginx 起动——pid 挪 /tmp，日志/缓存/html 目录
# 归属 nginx 用户。
# nginx:1.27-alpine 主配置的 pid 路径是 /run/nginx.pid（非 root 不可写），
# 逐行 sed 脆弱——直接换整份非特权主配置（pid /tmp、无 user 指令）。
# nginx-main.conf 无 ${VAR} 面，保持静态 COPY（见其头注）。
COPY nginx-main.conf /etc/nginx/nginx.conf
# 启动渲染需把渲染产物写进 conf.d（root 属主）→ 目录交 nginx 用户。
RUN chown -R nginx:nginx /usr/share/nginx/html /var/cache/nginx /var/log/nginx /etc/nginx/conf.d
COPY --from=builder /app/dist /usr/share/nginx/html
USER nginx
EXPOSE 8080
# busybox wget 探首页（SPA shell）。
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -q --spider http://127.0.0.1:8080/ || exit 1
# 启动即渲染模板再起 nginx（exec 前台）；渲染失败=配置不可用，容器快速失败。
CMD ["/bin/sh", "-c", "envsubst '$FD_WIRE_ADMIN_TOKEN' < /etc/nginx/conf.d/default.conf.template > /etc/nginx/conf.d/default.conf && exec nginx -g 'daemon off;'"]
