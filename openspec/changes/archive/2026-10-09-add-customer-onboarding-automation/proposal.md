# Proposal: add-customer-onboarding-automation

## Why

wire（柏讯）已生产上线，但接真实客户前 registry 侧有四个缺口：客户经 wire 入口**调不通自己的 cell 也看得见别家的**（R1）、余额预检对 99% 调用者不生效（R2，硬停形同虚设）、客户长命 key 只能管理员以自身权限代铸（R3，越权面）、计量归集的 registry 直读通道未定口径（R4）。现状开户 ceremony 是五步手工（Logto 建号→铸 key→secrets map→改 registry .env→验证），既不安全也不可扩展。

## What Changes

- **cell 授权（cell grant）全自动铸造**：每客户一份 registry 授权束——入口条目 `visibility=group-restricted` + 专属 scope 文档（`mcp-wire-cell-<cid>`，精确名匹配自己的 cell）+ 长命 key 组快照（`["wire-customers","wire-cust-<cid>"]`）。合成组**不经 Logto**（见 ADR 0005）；registry 侧零新代码，联动全走既有 admin API（`POST /api/servers/groups/import`，`create_in_idp:false`）。调用面与目录面随 scope 文档同时隔离（两者同源实时解析）。
- **预检调用者映射随开户自动建立**：registry 新增 `sub2api_caller_map` Mongo 集合 + admin 写入 API；preflight 改读集合（短 TTL 缓存）；新增 `PREFLIGHT_UNMAPPED` 开关，生产切 `deny`（未映射的非 admin 调用者拒绝；admin scope 平台凭据保持放行）。映射值=user_id（不复制 sk- 凭据材料）。
- **客户长命 key 铸造两步走**：第一步（零 registry 改动）cell-manager 自动化既有无浏览器铸造通道（自签 JWT 的 groups claims 即快照）；第二步 registry `POST /api/patch-keys` 增 admin-only 可选字段 `{username?, groups?}`（代铸指定归属与组），同船补 **admin-revoke**（现状 DELETE 跨用户一律 404，退租自动化的阻断点）。
- **计量归集定口径为 registry Mongo 直读**（拉模式）：cheap-1 mongo 只读账号 + tailnet 端口发布 + wire Secret 填值（Deployment 槽位已预留）。pod→tailnet 出站活探针为**闸门任务**，失败即降级 HTTP 推送方案。
- **cell-manager CLI 为开户唯一执行者**：secrets map 扩展 `customer_id → {username, sub2api_user_id}`；退租=吊销 key 先行→删 scope 文档→注销条目→deprovision，幂等可重跑、无宽限语义。
- 存量补正：drillcust001 演练 cell 条目与授权束按新约定重铸。

## Capabilities

### New Capabilities

- `cell-grant`: cell 在 registry 侧授权束的铸造、隔离不变量（客户只能调用/看见自己的 cell 与共享业务面）与回收时序。
- `caller-preflight-map`: 调用者→sub2api 账户映射的自动建立、读取时效与未映射策略（fail-closed 例外：平台凭据）。
- `customer-key-minting`: 客户长命 key 的铸造语义（指定归属与组、两个通道）与吊销（含 admin 代吊）。
- `metering-poll-channel`: 计量归集直读通道的配置要求、可达性闸门与降级条件。

### Modified Capabilities

（无——`openspec/specs/` 尚为空，本仓无既有能力被修改。）

## Impact

- **fd-wire / cell-manager**（主战场）：注册调用补 visibility/allowed_groups 字段、授权束铸造/回收子命令、2.1 铸 key 自动化、secrets map 扩展。
- **mcp-gateway-registry（跨仓，gitee/main @ dbf213e 基线，cherry-pick 纪律）**：R2 的 caller-map API + preflight 集合读取 + `PREFLIGHT_UNMAPPED` 开关；R3 第二步的 mint 字段 + admin-revoke。registry 侧不改动 R1 任何代码。
- **cheap-1 infra**：mongo 只读账号（限 `mcp_registry.audit_events_default`）、compose 端口发布到 tailnet IP（单滚 `--no-deps --no-build`）。
- **fd-wire api（cheap 集群 fd-prod）**：Secret `fd-wire-env` 填 `REGISTRY_MONGO_URI/DB`；无代码改动。
- 不动：`SSRF_ALLOWED_CIDRS` 与 `EGRESS_REGISTRY_INTERNAL_URL` 两行 .env；patch key 通道本身；控制台注册默认 disabled 语义；CN 镜像配方（dbf213e 已含）。
