# registry 侧需求：cell 隔离与 wire 接客前配套（2026-10-08 整理）

> **交付对象**：registry 专属 session（cheap-1 `/opt/mcp-gateway-registry`，gitee
> `FindDataTechnology/mcp-gateway-registry`，分支 `fd-backend-auth-fallback`，
> HEAD dbf213e 定版）。本文档自包含：每个需求的现状证据、期望行为、验收判据
> 都已给全，不需要读本 session 的上下文。
>
> **背景**：wire（柏讯 B2B）已生产上线：客户 cell（企业隔离容器）由
> cell-manager 全自动 provision 并注册进 registry；平台服务凭据（patch key
> `wgk-`）与入口标准 MCP 形态均已闭环。**接真实客户前，registry 侧有四个
> 缺口**，按优先级排列如下。
>
> **2026-10-08 状态更新（已复验）**：R1、R2 已由另一个 session 交付
> （`add-customer-onboarding-automation`，per-customer scope + Mongo
> caller-map + `PREFLIGHT_UNMAPPED` 开关），本 session 已按生产实态复验并
> 收口，详见各节末「交付状态」。R3、R4 仍待办。



---

## R1【P0】每客户 cell 条目隔离（现在互相可见、且谁都调不通自己的 cell）

### 现状（2026-10-08 生产实查）

- 每个客户 cell 是 registry 里的一个独立 server 条目，路径约定
  `fd-wire-cell-<customer_id>`（cell-manager `registry_path`）。
- **调用面（P0 的一半）**：wire 客户组的 scope `mcp-business-wire-execute`
  （group_mappings=`["wire-customers"]`）的 `server_access` 只放行
  `fd-find-data-business-mcp`——**没有任何 cell 条目**。同时 auth-server 的
  server 名匹配 `_server_names_match`（`auth_server/server.py:2184`）只支持
  `*` 全通配或全名匹配，**没有前缀通配**——没法用一条规则放行
  `fd-wire-cell-*`。结果：**客户经入口根本调不到自己的 cell**（初始化握手
  都过不了 scope 校验）。当前演练 cell 之所以通，是因为我们全用
  `mcp-registry-admin` 组的平台凭据。
- **发现面（P0 的另一半）**：cell-manager 走控制台
  `POST /api/servers/register`，不带 `visibility`/`allowed_groups`
  （server_routes.py:4313 有这两个 Form 字段但客户端没传）→ 条目落库为
  `public`。`mcp_servers_default` 实查：**drillcust001 条目无 visibility
  字段（=public）、无 allowed_groups**——任何客户在目录/列表里能看见
  别家企业的 cell 条目（企业名就挂在 display_name 里：「柏讯 cell ·
  <customer_id>」）。

### 期望行为

1. 客户（wire-customers 组）经入口**只能**调用 `fd-wire-cell-<自己的
   customer_id>`，不能调用其他任何客户的 cell，也不能看见别人的 cell 条目。
2. scope 授权能随 cell 开通/注销自动生效——**不希望每开一个客户都手改
   scope 文档**（现状 scope 是 Mongo 手工文档）。

### 建议方案方向（三选一，session 内定）

- **A（推荐）：per-customer scope 文档 + cell-manager 注册时联动创建**。
  cell-manager 注册 cell 时顺带（经新 internal API 或直接 Mongo）创建
  `mcp-wire-cell-<customer_id>` scope 文档：`server_access=[{server:
  "fd-wire-cell-<cid>", methods:["all"], tools:["all"]}]`、
  `group_mappings=["wire-cust-<cid>"]`（Logto 每客户也建一个组/或在
  patch key 铸造时把该组快照进 key 的 groups）。缺点：Logto 组管理面
  要跟开（M2M /api/v2 曾 404，Admin Console UI 可用）。
- **B：`_server_names_match` 支持前缀通配**（`fd-wire-cell-*`）+ 别的机制
  挡跨客户（每客户 still 需要 per-customer 边界，前缀通配只解决「放行
  家族」，**单靠它不够**——放行了所有人的 cell）。需与 per-customer
  判定组合（如 `server_access` 支持 `"server_prefix": "fd-wire-cell-"`
  且校验请求 server 名后缀 == 调用者 username/customer_id）。实现点在
  auth_server/server.py:2184 与 scope repo。
- **C：virtual server 独立入口**：每客户发一个 virtual server
  （`virtual_servers_default` 先例：lex-legal 有
  `allowed_groups=["mcp-lex-execute","mcp-law-bench-execute"]`），cell
  作为其 backend。看 virtual 路径是否已把 group 门禁接到 /validate——
  **session 需先实证这一点**（我们只查到 virtual 文档有 allowed_groups
  字段，没验证调用面是否真门禁）。

### 验收判据

- [ ] 客户身份 A（wire-customers 组 + 其 cell 存在）经入口
      `initialize` → 200；调 `fd-wire-cell-<B 的>` → 403/404；
      目录 `GET /api/servers` 看不见 B 的 cell。
- [ ] cell-manager provision 一个新 cell 后，**无需任何手工 scope/组改动**，
      新客户的 key 即可调通自己的 cell（全自动闭环延续）。
- [ ] 平台服务凭据（wire-platform-service）不受影响（admin 组全通）。

### 交付状态（2026-10-08 复验：通过）

采纳了方案 A（per-customer scope）。生产实态：

- `mcp-wire-cell-drillcust001` scope 已建
  （`groups=["wire-cust-drillcust001"]`，
  `server_access=[{server:"fd-wire-cell-drillcust001",...}]`）
- 客户 key（代铸，groups 双组 `wire-customers` + `wire-cust-<cid>`）
  实弹：本企业 cell 200 / 共享业务面 200 / 别家 cell 不可达 /
  平台内部 server 403；
  **目录恰见 2 条**（自己的 cell + 共享业务面），无别家条目——
  「互不可见、互不可调」成立。
- 遗留小偏差：跨企业路径返回 **405**（nginx 无该 location → 控制台兜底）
  而非 403；对外文档写的是 403，需一致化（见下节 R1a）。

---

## R2【P0】预检硬停的调用者映射（现在对 99% 客户不生效）

### 现状

auth-server preflight（`auth_server/preflight_quota.py`）strict 模式已开
（`.env`: `PREFLIGHT_ENABLED=true`, `PREFLIGHT_MODE=strict`），但余额探针
的调用者映射是手工 env：`SUB2API_CALLER_MAP={"8tajhoprelg0": "56",
"lexdemocustomer": "56"}`——**只映射了两个身份**。`_resolve_target`
（preflight_quota.py:250）对**不在 map 里的调用者直接 skip→pass**（设计如此，
fail-open）。即：**除了这两个测试身份，任何客户（包括未来的付费客户）经
入口调用完全不受余额预检约束**——额度耗尽照常调用，wire 的 402 硬停
（wire-platform-v1 4.4）形同虚设。

### 期望行为

新客户开户（Logto wire-customers 组 + 铸 key）后，其入口调用自动受
sub2api 余额预检约束；映射关系随开户自动建立，不靠手工改 `.env`。

### 建议方案方向

- **A（推荐）：映射落 Mongo + 开户联动**。preflight 读
  `sub2api_caller_map` 集合（username → sub2api user id / `key:` 前缀），
  cell-manager 或 wire api 的开户流程写入；`.env` 的 `SUB2API_CALLER_MAP`
  保留为覆盖层（或退役）。注意 preflight 现有 `_caller_map` 是 env memo
  （preflight_quota.py:204），要加集合读取与短 TTL 缓存。
- **B：sub2api 用户名直查**。preflight 直接按 `sub`（= username）查
  sub2api 用户目录（wire 平台已按身份代建账户），免映射——需确认 sub2api
  admin API 有按用户名查询端点。

### 验收判据

- [ ] 新映射客户（余额>0）调用放行；余额耗尽客户调用 → 402
      INSUFFICIENT_BALANCE（且 wire 侧结算一致）。
- [ ] 未映射客户（新开户漏配）的行为明确：要么 fail-closed（推荐，
      加 `PREFLIGHT_UNMAPPED=deny` 开关），要么至少告警——**现状是静默
      放行，必须改**。

### 交付状态（2026-10-08 复验：通过，且已收口为 deny）

采纳了方案 A（Mongo 集合 `sub2api_caller_map` + admin API
`PUT/GET/DELETE /api/sub2api-caller-map`），并新增
`PREFLIGHT_UNMAPPED=pass|warn|deny` 开关。

本 session 复验 + 收口：

- 交付后生产默认仍是 `pass`（未映射放行）；本 session 已切 **`deny`**
  并重载 auth-server（备份 `.env.bak-unmapped-20261008`；prebuilt 形态用
  `docker compose -f docker-compose.prebuilt.yml up -d auth-server
  --no-deps --no-build`）
- 实弹：摘掉 drillcust001 映射 → 真实客户 key 调共享面
  **402 CALLER_NOT_MAPPED**（带中文说明「未完成计费开户映射」）；
  恢复映射后 cell/共享面均回 200（TTL 30-60s 内生效）
- admin 语义（平台服务凭据）豁免直通不受 deny 影响——符合双轨设计
- 调用顺序正确：scope 授权先于计费判定（403 不会被 402 掩盖）

---

## R3【P1】客户凭据自助铸造与吊销（免人工 ceremony）—— 已交付（代铸+admin 代吊，sha-1ffa7cb 起，生产实弹 201→200→401；wire 侧 cell-manager mint-key/revoke-key 已接）

### 现状

- patch key 铸造 `POST /api/patch-keys` 已可用（无浏览器通道：自签短命
  JWT → 铸造 API，配方已落 `fd-wire/docs/service-credentials.md` 2.1 节），
  但铸造身份是**调用者本人**——平台管理员替客户铸造时，key 的 groups
  快照取的是**管理员**的组（mcp-registry-admin），**没有 per-customer
  限定**：现在客户 key 若由管理员代铸，key 与管理员同权（能看见全部
  server）。
- 客户自助铸 key 需要控制台会话（浏览器 OAuth2 登录）。wire 控制台
  尚无「API keys」页（wire 侧 backlog）。

### 期望行为

客户 key 的权限恰为该客户应得的（business-mcp + 自己的 cell），铸造/
吊销不需要管理员手工操作；最好客户在 wire 控制台自助。

### 建议方案方向

1. **铸 key 时支持指定 groups**：`POST /api/patch-keys` 加可选 body 字段
   （仅 admin 可用，如 `groups: ["wire-customers"]` 或 per-customer 组），
   mint_key 快照指定的组而非调用者组（patch_key_service.mint_key 现有
   `groups` 参数来自调用者 ctx，server_routes.py:77-83）。
2. wire api 侧把「铸造客户 key」接进开户流程（调 registry 新通道），客户
   key 明文经安全信道给客户。

### 验收判据

- [ ] 能以平台服务身份铸出一把「groups=wire-cust-<cid>」的客户 key，
      该 key 只能看见/调用 business-mcp + 自己的 cell。
- [ ] 吊销即时（`DELETE /api/patch-keys/{key_id}` 已有，验证即可）。

---

## R4【P1】metering 归集面：registry Mongo 直读的稳定性

### 现状（信息同步，非急缺）

wire api 的计量入账驱动（`fd-wire/api/fd_wire_api/metering/driver.py`，
ecosystem-bridge 3.1）按 `REGISTRY_MONGO_URI` 直读 registry 的
`audit_events_default` 集合（300s 轮询）。**该 Secret 键当前为空**——
驱动 poll 段自动跳过（HTTP 推送路径喂同表，目前由其他会话验证）。接客后
计量靠这条链入账本，需要确认。

### 期望

决定一个正式口径：a) 配 `REGISTRY_MONGO_URI`（从 cheap-1 mongo 凭据生成
只读账号 + `mongodb://<user>:<pass>@<host>:27017/mcp_registry?authSource=admin`
——注意 mongo 端口只绑 cheap-1 本机/内网，需要经 k8s 可达路径，可能要走
NodePort 或 tailscale IP）或 b) registry 侧加 HTTP 推送（audit 流 → wire
internal endpoint）。**session 里拍板并落地**。

### 交付状态（2026-10-08 复核：已落地，待配 Secret 后生效验证）

直读口径已选定并落地（切片二 5.1-5.4）：

- 只读账号已建（集合级角色 `wireMeteringRO`，仅
  `mcp_registry.audit_events_default` find 权限；写/他集合实证 Unauthorized）
- mongo 绑 tailnet `100.64.0.11:27017`（公网不可达），闸门探针从 fd-prod
  临时 pod 实探**通过**
- api 镜像已带 pymongo（sha-5d3c2fa），`REGISTRY_MONGO_COLLECTION` 已配
  `audit_events_default`
- **仍缺**：`fd-wire-env` 的 `registry_mongo_uri` / `registry_mongo_db`
  两键当前为空 —— 直读驱动 poll 段自动跳过（HTTP 推送路径喂同表）。
  待配后验证入账时延（他们实测水位追平后 7 秒入账）。

---

## 附：已解决、不需要动的（防重复劳动）

- **patch key 通道已可用**：`PATCH_KEY_AUTH_ENABLED` 默认开；无浏览器铸造
  配方在 fd-wire/docs/service-credentials.md；`/api/servers/toggle` 接受
  patch key（`/api/toggle/<path>` 不接受——那是 enhanced_auth 会话专用）。
- **控制台注册默认 disabled**：cell-manager 已带 register→toggle 启用步
  （fd-wire 30ba42d），registry 侧无需改。
- **SSRF 白名单**：`.env` 的 `SSRF_ALLOWED_CIDRS` 已合并为
  `100.64.0.0/10,192.168.32.0/20` 单行（10-07 曾因重复行 last-wins 覆盖丢
  mesh 网段）；`EGRESS_REGISTRY_INTERNAL_URL=http://registry:8080` 已修正。
  别动这两行。
- **cn 镜像重建**：dbf213e 已含（apt/gitee/ghfast.top 三镜像持久化 +
  OPTIONS preflight 双模板变体）。

## 附：环境事实速查

- registry 栈：cheap-1 `/opt/mcp-gateway-registry`（compose），registry
  app=127.0.0.1:7860（nginx 8080 唯一对外 7860 路径 =
  `/_egress_internal/egress-token`）、auth-server:8888、mongo
  `mcp-mongodb`（root 凭据在容器 env `MONGO_INITDB_ROOT_*`）。
- 审计/计费数据：`mcp_registry` 库，`audit_events_default`（TTL 7 天
  ——**R4 若选直读要考虑 TTL 窗口**，300s 轮询足够）、`mcp_scopes_default`
  （scope 文档）、`mcp_servers_default`（server 条目）、
  `virtual_servers_default`（虚拟入口）。
- 平台服务凭据：`wire-platform-service`（两把 wgk-，明文 cheap-1
  `/opt/cell-manager/secrets/`，0600）。
- cell 条目约定：`fd-wire-cell-<customer_id>`（cell-manager
  `CELL_NAME_PREFIX`）；演练 cell `fd-wire-cell-drillcust001` 在跑（可作
  R1 验收对象）。
- 提交纪律：registry 改动走 gitee 分支（当前 `fd-backend-auth-fallback`），
  cheap-1 /opt 是 ff 部署；重建镜像参考 dbf213e 的 CN 镜像配方；compose
  操作 `docker compose up -d --no-deps --no-build`（alpine 依赖镜像国内
  拉不到，禁 --build）。

---

## R1a【P2】跨企业路径的错误码一致化（新增，复验发现）

### 现状

客户 key 请求一个**不存在/未授权**的 cell 路径（如
`/fd-wire-cell-cust999/mcp`）→ **405 Method Not Allowed**
（nginx 无该 server 的 location，落到控制台兜底路由 `location /`，
FastAPI 对 POST /mcp 无该方法）。而对外文档
`docs/customer-onboarding-integration.md` 第 4 节承诺的是 **403**。

### 期望

跨企业（或不存在）的 cell 路径返回 403（授权语义），不要返回 405
（路由语义）——agent 侧按文档把 403 当「越权/写错路径」，405 会被误判为
「端点不支持」从而重试。

### 方案方向

registry 生成的 per-server location 之外，加一条通配/默认 MCP 前缀路由
（`/fd-wire-cell-*`）返回 403，或让控制台兜底对 `/mcp` 后缀路径返回 403。
注意不要影响 business-mcp 等既有条目的路由。

### 验收判据

- [ ] 客户 key 请求别家 cell 路径 → 403（且 body 带授权语义说明）
- [ ] 本企业 cell 与共享业务面仍 200；未注册路径的控制台其他路由不受影响

### 交付状态（2026-10-09 复验：通过）

另一 session 已修：`location ^~ /fd-wire-cell-` → 403 JSON（授权语义），
已注册 cell 靠最长前缀胜出不受影响。本线实弹：未注册 cell → **403 +
`{"error":"FORBIDDEN","message":"该 cell 不在你的授权范围或不存在"}`**，
own cell / 共享面仍 200。对接方文档第 7 节的临时提示（405 按 403 处理）
可在下次文档整理时移除。

---

## R5【P0】退租链路的 scope 删除被边缘拦（405）→ 授权面残留

### 现状（2026-10-08 生产实证，acme001 退租）

cell-manager 退租编排最后一步删授权 scope：
`DELETE /api/iam/groups/mcp-wire-cell-<cid>` → **405 Method Not Allowed**，
响应头 `Allow: GET`（经 Caddy/边缘，nginx 侧端点存在：
`registry/api/management_routes.py:847`）。

后果（严重）：teardown 抛错 → `deprovision` 整步失败 → **容器还在跑、
registry 条目还在线、caller-map 映射还在库**，退租卡死在半途，客户授权面
完整残留。

**wire 侧已做的缓解**（不等 registry 修）：cell-manager 的
`teardown_registry_face` 改为「删 scope 失败不中止」——以 `scope_error`
如实入回执与审计，条目注销与后续清理照做（残留可见可补优于卡死半途）。
**但 scope 文档本身仍在库里**，需 registry 侧解决根因。

### 期望行为

`DELETE /api/iam/groups/{group_name}` 经公网入口可达（与
`POST /api/servers/remove`、`DELETE /api/sub2api-caller-map/{username}`
同等放行——后两者实测可用，仅 iam/groups 的 DELETE 被拦）。

### 验收判据

- [ ] admin patch key 经入口 `DELETE /api/iam/groups/<测试组>` → 200
      （非 405，`Allow` 头含 DELETE）
- [ ] 退租演练重跑：scope 文档被真删（`mcp_scopes_default` 查不到），
      回执无 `scope_error`
- [ ] 不影响 GET/POST/PATCH 等其它方法的现有行为

### 交付状态（2026-10-09 复验：通过 + 本线补两处未覆盖路径）

另一 session 已修（三层根因：公网 DELETE /api/iam/groups 被 GET-only 路由
遮蔽 405 → /api/management 面因 IdP 同步删除 502 → /api/internal/* 被 nginx
`^~` 劫去 8091）。修法 = cell-manager `InternalScopeDeleter` 走
`POST /api/internal/delete-group` + `delete_from_keycloak=false`（只删 Mongo
不碰 Logto）+ nginx 精确 location 放行。

本线复验时实弹抓到**两个未覆盖路径**（e2e001 全生命周期）并修复：

1. `manager.purge`（retention 到期批量清除）仍调旧通道 → purge-expired
   整步失败、到期清除卡死。修：purge 走 `_delete_scope_resilient`
   （优先 InternalScopeDeleter，与 teardown 同通道）
2. **退租不摘预检映射**：teardown/purge 都不删 caller-map → username
   永久占位（e2e001 实证残留）。修：`HttpRegistryClient.delete_caller_map`
   + teardown 摘除（失败以 `caller_map_error` 留痕不中止）

终验（final001 全生命周期）：grant→provision→deprovision→purge 零残留
（caller-map/scope/条目/容器/卷全清）；回归 drillcust001 与平台凭据
cell/biz 均 200。cell-manager 125 passed。

## R6【P1】预检把「计费面查不到用户」误分类为额度不足

### 现状

映射已生效但 sub2api 里没有对应账户时（开户漏建账户 / 用户 id 填错），
preflight 返回 `402 INSUFFICIENT_BALANCE`，审计 reason 记的是
`readUser refused (code=404, HTTP 404)`。

fail-closed 方向是对的（拒了），但**语义误导**：对接方文档说 402 =
「需要充值」，客户会去充值；实际是开户配置缺口（应联系平台）。审计日志
里的 404 才是真相，对接方看不到。

### 期望行为

计费面查不到用户（404）→ 独立错误码（如 `402 BILLING_ACCOUNT_MISSING`
或 `409`），消息明确指向「开户配置缺口，请联系平台」，与
`INSUFFICIENT_BALANCE`（真没钱）区分开。

### 验收判据

- [ ] 映射存在但账户不存在 → 返回具名的账户缺失错误（非
      INSUFFICIENT_BALANCE），审计 reason 一致
- [ ] 真余额耗尽仍返回 INSUFFICIENT_BALANCE（不回归）
- [ ] 对接方文档同步补该错误码语义

### 交付状态（2026-10-09 复验：通过）

另一 session 已修：readUser 404 → 独立 verdict → `402 BILLING_ACCOUNT_MISSING`
（中文消息指向开户缺口）+ audit `rejected_billing_account_missing`；真余额
耗尽仍 `INSUFFICIENT_BALANCE`（单测护栏）；对接方文档已补。本线实弹
（e2e001，映射在而账户不存在）：返回 BILLING_ACCOUNT_MISSING、审计
`readUser 404: billing account not found`。
