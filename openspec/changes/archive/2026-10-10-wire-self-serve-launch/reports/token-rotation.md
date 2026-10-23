# 5.3 令牌轮换记录（2026-10-09）

## 背景

三枚静态令牌（`fd_wire_admin_token`、`fd_wire_internal_token`、`metering_ingest_token`）的旧值曾进入会话日志（INFRA-NOTES 10-05 段、/tmp/fd-wire-tokens.txt），按 wire-self-serve-launch 任务 5.3 全量轮换。

## 执行（值全程不进会话日志）

1. cheap-1 服务端 `openssl rand -hex 24` 生成三枚新值 → root-only `/opt/fd-wire-tokens.txt`（600）。
2. 旧值全盘搜索消费点：唯一外部消费点=`/opt/mcp-gateway-registry/.wire-ingest-token`（registry 侧 metering HTTP 推送），已服务端同步更新（48 字符）。
3. 本机以 shell 变量透传（不回显）`kubectl patch secret fd-wire-env`，三键一次合入。
4. `rollout restart` fd-wire-api + fd-wire-web（web 经 nginx envsubst 注入 admin 令牌，重启才吃新值）。

## 验证（生产实弹）

- 旧泄露 admin 令牌 → `/internal/admin/customers` **401**
- 旧 internal 令牌 → `/internal/quota/*` **401**
- 新 internal 令牌 → `/internal/quota/whoever` **404 {"detail":"caller whoever 未映射到客户"**}（鉴权通过，仅 caller 不存在——fail-closed 口径正确）

## 附带事实

- INFRA-NOTES 旧 T1/T2 明文已替换为指针（→ cheap-1 /opt/fd-wire-tokens.txt）；cheap-1 /tmp/fd-wire-tokens.txt 已删。
- registry 行数门探针（3.2，gate=off 未灰度）上线时 `FD_WIRE_INTERNAL_TOKEN` 取 /opt/fd-wire-tokens.txt 的 fd_wire_internal_token 值。
- admin 双闸 `FD_WIRE_ADMIN_BEARER_GATE=on` 已随 sha-7fae9e5 生效：内部后台在 Logto wire-admin 角色配好前暂不可用（fail-closed 方向安全，见 4.1）。
