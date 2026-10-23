# Design: add-customer-onboarding-automation

## Context

registry 定版为 gitee/main @ dbf213e（cheap-1 `/opt/mcp-gateway-registry` compose 栈）。2026-10-08 对生产代码逐点实证的机制事实（全部带 dbf213e 行号核验）：

1. **三层实时解析**：组→scope（`map_groups_to_scopes` → `get_group_mappings_bulk`，单条 `$in`）、scope→server_access（`get_server_scopes`）、目录可见列表（`accessible_servers`，auth/dependencies.py:454,608）全部每请求实时查 Mongo，无缓存、无 public 旁路。联动写的授权文档**下一请求即生效**；`/internal/reload-scopes` 只是兜底。
2. **组冻结在 key、scope 实时**：patch key 的 groups 是铸造时快照（auth_server/server.py:2930 注释明示），scope 名每次按当前映射解析。`map_groups_to_scopes` 不校验组在任何 IdP 存在 → **合成组 `wire-cust-<cid>` 无需 Logto**。
3. **铸造通道已支持指定组**：无浏览器铸造（自签短命 JWT）的 `groups` claims 即快照来源（fd-wire docs/service-credentials.md §2.1 配方，cheap-1 实测）。
4. **一处可用的一次性授权写入端点**：`POST /api/servers/groups/import`（admin 门 + Bearer 免 CSRF，`server_access`+`group_mappings` verbatim upsert）。注意坑：`POST /api/iam/groups` 会**忽略** client 的 `group_mappings`（强制 `[name]`）；import 端点 `create_in_idp` 默认 true 会尝试去 Logto 建组（非致命）——**必须传 `false`**。
5. **吊销 owner 限定**：`DELETE /api/patch-keys/{id}` 跨用户一律 404（防探测），admin 无法代吊——退租自动化的阻断点。
6. **目录隔离随 scope 自动达成**：listing 按 accessible_servers（scope 推导）严格过滤；`visibility=group-restricted` + `allowed_groups` 只额外覆盖 search/ans 类 surface（`user_can_access_server_from_doc`）。
7. **wire api 运行环境**：cheap 集群 fd-prod（NodePort 31883 ← cheap-1 Caddy）；`REGISTRY_MONGO_URI/DB` Secret 槽位已预留（fd-wire-api.yaml:82-87，optional）。既有 tailscale 路径全是 cheap-1→集群入站方向，**pod→cheap-1 tailnet 出站无先例证据**（集群节点在 tailnet 上，大概率通，需活探针）。
8. cell-manager 是 **CLI**（argparse 单发）不是常驻服务，已持 admin wgk-、是唯一能建 cell 容器的角色。

## Goals / Non-Goals

**Goals:**
- 接客闸门（切片一）：R1 隔离 + R2 预检映射 + R3 第一步（JWT-claims 铸 key 自动化）全绿，drillcust001 存量按新约定重铸。
- 配套收尾（切片二）：R3 第二步（代铸 API + admin-revoke）+ R4 直读通道（含探针闸门与降级）。
- registry 侧改动最小化：R1 零代码；R2/R3② 各一小块，走 gitee 线 cherry-pick。

**Non-Goals:**
- 客户在 wire 控制台自助铸/吊 key（wire 侧既有 backlog，不本次）。
- 改 `_server_names_match` 语义、改 virtual server 路由（B/C 方案已否决）。
- Logto 侧 per-customer 组管理（合成组不经 IdP，ADR 0005）。
- 计量推送生产者（registry 侧 forwarder）——仅作 R4 降级备选，探针失败才启动。
- connector key 的铸造（连接器向 wire api 认证，不经 registry，不受本设计影响）。

## Decisions

### D1 隔离机制 = per-customer 授权束（方案 A 改进版）

每客户三件套：入口条目 `visibility=group-restricted, allowed_groups=["wire-cust-<cid>"]` + scope 文档 `mcp-wire-cell-<cid>`（`server_access=[{server:"fd-wire-cell-<cid>",methods:["all"],tools:["all"]}]`、`group_mappings=["wire-cust-<cid>"]`）+ key 组快照 `["wire-customers","wire-cust-<cid>"]`。

- 组载体不经 Logto：合成组只活在快照与 scope 文档（ADR 0005）。共享业务面继续走既有 `wire-customers → mcp-business-wire-execute` 映射，不动共享文档。
- 否决 B（前缀通配+身份后缀校验）：给 lex-legal 刚修过的安全热路径（`_server_names_match`）引入新语义，精确匹配已够。
- 否决 C（virtual server）：已实证 Lua 层门禁真实（any-of）但不省授权文档 → 纯增加间接层。

### D2 开户编排 = cell-manager CLI 单执行者

cell-manager 新增开户/退租子命令，一跳完成：注册（带 visibility 字段）→ import 授权文档（`create_in_idp:false`）→ 铸 key（D4 第一步通道）→ 写预检映射（D3 API）。输入 = secrets map 扩展项 `customer_id → {username, sub2api_user_id}`（每次运行现读，无重启）；wire api 继续拥有 sub2api 建户，把 user_id 交到 map（现状手工 ceremony 第 3 步的通道延续，本次不建 wire→cell-manager API）。

### D3 预检映射 = Mongo 集合 + registry admin API

- 新集合 `sub2api_caller_map`（mcp_registry 库）：`{username → sub2api_user_id}`。值=user_id 而非 sk-key：不复制凭据材料；探针走既有 admin readUser 路径（`SUB2API_ADMIN_KEY` 已配置）。
- 写入端点：registry app 新 admin API（`nginx_proxied_auth` + `_require_admin` + audit，与 `/iam/groups` 同模），cell-manager 持 admin wgk- 直调。
- preflight 改造（auth-server）：`_caller_map` 读集合 + 30-60s TTL 缓存；env `SUB2API_CALLER_MAP` 降级为覆盖层（env 命中优先），迁移完成后退役。
- 新开关 `PREFLIGHT_UNMAPPED=pass|warn|deny`（默认 pass 保现状）：deny 下未映射的**非 admin** 调用者拒绝；admin scope（`mcp-registry-admin`，即平台凭据）保持 skip-pass——平台轨计量不归客户余额门。
- 否决 B（sub2api 用户名直查）：admin API 有无按用户名查询端点未验证，且引入 registry→sub2api 每请求依赖；wire 建户时手里已有 user_id，写入一次即稳。

### D4 铸造两步走

- **第一步（切片一，零 registry 改动）**：cell-manager 在 cheap-1 本机自动化 §2.1 配方——自签短命 JWT（`sub=<客户 username>`、`groups=["wire-customers","wire-cust-<cid>"]`，10 分钟有效、用完即弃）→ `POST /api/patch-keys`。信任等级与现状相同（cell-manager 已持 admin wgk- 且同机）。
- **第二步（切片二，registry 小改）**：`POST /api/patch-keys` 增 admin-only 可选字段 `{username?, groups?}`——**两个字段必须成对设计**：只有 `groups` 的话，集群侧代铸的 key owner 会错记成 admin，吊销/列表归属全乱。同船：DELETE 加 admin 旁路（admin 可吊任意 key，`revoke_key` 的 username 校验对 admin 放行），修复 D-Context#5 的退租阻断点。

### D5 计量通道 = 直读 + 探针闸门 + 推送降级

- 直读（正道）：cheap-1 mongo 只读账号（仅 `mcp_registry.audit_events_default` 读权限）→ compose 端口发布到 tailnet IP（单滚 `--no-deps --no-build`，预告短停）→ wire Secret 填 `REGISTRY_MONGO_URI/DB`。audit 7 天 TTL 窗口 = wire 停机兜底余量，300s 轮询安全。
- 闸门：从 cheap 集群 pod 对 cheap-1 tailnet IP:27017 做一次**真实出站探针**（如 `kubectl run` 临时 pod + mongosh/nc），通过才填 Secret 开闸。
- 降级：探针失败 → 改推口径（registry 侧加 audit forwarder 推 wire 既有 `POST /internal/metering/events`），届时按事实单独立项，不在本 change 预建代码。

### D6 退租时序

吊销 key（下一次调用即拒）→ 删授权文档（既有 DELETE）→ 注销入口条目 → cell-manager deprovision cell 容器与数据根。无宽限语义（与「吊销即时生效」词条一致），全流程幂等可重跑。

## Risks / Trade-offs

- [探针失败：pod→tailnet 出站不通] → 降级推送口径（D5）；探针是切片二闸门任务，不阻塞切片一。
- [import 端点 `create_in_idp` 误传 true → Logto 建组副作用] → cell-manager 封装内固定 false；失败本就非致命（吞异常）。
- [合成组与 Logto 组名撞车（未来真在 Logto 建 wire-cust-*）] → 组名空间约定 `wire-cust-<cid>` 专属快照轨道，ADR 0005 记录在案；scope 文档的特权写防护继续兜底（不能把 wire-cust-* 映到 admin scope）。
- [preflight TTL 缓存窗口内新映射未生效] → 30-60s 窗口，开户后首调偶发 402/deny 可重试；验收判据给「写入后有界生效」留了分钟级窗口。
- [drillcust001 存量 public 条目] → 存量补正任务（重注册带 visibility 字段 + 补授权文档 + 换组快照正确的 key）。
- [registry 双线谱系] → 所有 registry 改动基于 gitee/main @ dbf213e 新分支，cherry-pick 纪律（同 lex-legal da4c6cf7 先例）；本地 `fd-key-group-fallback` 是另一谱系且有并行会话在用，**勿动其树**。
- [mongo 暴露面扩大] → 只读账号 + 限库限集合 + tailnet 网段 + authSource=admin；不发布公网。

## Migration Plan

1. 切片一（fd-wire 仓 cell-manager + 既有 registry API，**无 registry 发版**）：实现 → drillcust001 重铸演练 → 验收判据全跑。
2. 切片二 registry 部分（gitee 分支）：R2 端点+preflight 改造、R3② 字段+admin-revoke → 测试随迁 → 重建镜像（CN 镜像配方，dbf213e 已含）→ cheap-1 ff + `up -d --no-deps --no-build` → 探针/验收 → gitee main 合并。
3. 切片二 infra 部分：mongo 只读账号 + tailnet 发布 + pod 探针 → 通过则填 Secret 开闸直读；失败则挂起直读、推送口径另立 change。
4. 回滚：R2/R3② 的 registry 改动按 gitee 单分支 revert + 重滚镜像；`PREFLIGHT_UNMAPPED` 回 pass 即回到现状语义；切片一 cell-manager 侧改动不影响 registry 现有行为（新字段/新文档均为增量）。

## Open Questions

- `sub2api_caller_map` 写入 API 的具体路径命名（如 `/api/sub2api-caller-map` vs 并进既有 iam 面）——实现时定，不影响任何行为契约。
- wire api 把 `{username, sub2api_user_id}` 交到 cell-manager secrets map 的更新动作，本次保持手工（ceremony 第 3 步通道延续）；后续若建 wire→cell-manager API 另立 change。
