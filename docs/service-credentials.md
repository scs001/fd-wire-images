# 平台服务凭据发放约定（wire-platform-v1 · 8.2 缺口修复 G2）

壹座 / 萬星生态与 wire 平台内部服务要经 **wire 入口**（registry wire 门，
`mcp.finddatatech.cloud`）调用 MCP 工具时，一律持**平台服务凭据**（服务身份的
长命 key）——这类调用在计量上走 `credential_track=platform`（归集不结算），
并按服务身份前缀落到 `source=yizuo / wanxing / platform-internal` 桶
（`GET /api/admin/usage/overview` 双维可查）。本文约定这类身份怎么命名、
发放、轮换，以及发放后必须回填 wire 集群的联动步骤。

与客户凭据（per-customer 长命 key，计费到客户账单）互斥：客户身份**不得**
进入下述平台名单；两轨凭据在 cell 内的注入机制见文末。

## 1. 服务身份命名规范：`<line>-<service>`

| 前缀 | 归属 | 示例 |
|---|---|---|
| `yizuo-` | 壹座生态服务 | `yizuo-market`（壹座市场）、`yizuo-agent-01`（壹座 agent 池） |
| `wanxing-` | 萬星生态服务 | `wanxing-fleet`（萬星调度） |
| `platform-` | wire 平台内部 | `platform-agent`（wire AI 探索，即 api 侧 `AI_PLATFORM_CREDENTIAL` 的身份） |

规则：

- 形态 `<line>-<service>`，全小写、`[a-z0-9-]`；`<service>` 用途语义化（服务名 / agent 池编号）。
- **前缀即计量来源**：`PLATFORM_IDENTITY_SOURCE_MAP` 按前缀通配归桶
  （`yizuo-*=yizuo,wanxing-*=wanxing,platform-agent=platform-internal`），
  新增生态线 = 新前缀 + 映射条目，不改代码。
- 一个服务一个身份：不共用、不复用客户身份；服务下线即吊销并从名单摘除。

## 2. 发放通道

registry（mcp-gateway-registry fork，cheap-1 compose，基址
`https://mcp.finddatatech.cloud`）的 wire 门只认 header 凭据：
`Authorization: Bearer <key>`（auth_server `/validate` 子请求校验；不支持
URL query 传 key）。

### 2.1 首选：patch key（`wgk-`，无浏览器铸造，2026-10-07 起可用）

**patch key 是服务凭据的正解**——registry 定版镜像已默认开启
（`PATCH_KEY_AUTH_ENABLED` 默认 true、铸出前惰性）：

- 形态 `wgk-` 前缀（43 位 url-safe 随机），**永不过期**，库里只存 SHA-256
  哈希（明文仅在铸造响应中出现一次），吊销即时生效（下次调用即拒）
- 同时被 **MCP 入口**（`/<server>/mcp`）与 **registry `/api/`** 双面接受
- 每用户上限 20 把；吊销走 `DELETE /api/patch-keys/{key_id}`

**无浏览器铸造通道**（服务身份无人值守的正解，cheap-1 实测配方）：

```bash
# 在 cheap-1 /opt/mcp-gateway-registry 下执行（SECRET_KEY 不出主机）
python3 - <<'PY'
import base64, hmac, hashlib, json, time, urllib.request
secret = [l.split("=",1)[1].strip() for l in open(".env") if l.startswith("SECRET_KEY=")][0].strip("\"'")
b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
now = int(time.time())
claims = {"iss":"mcp-auth-server","aud":"mcp-registry","sub":"<service-username>",
  "groups":["mcp-registry-admin"],
  "scope":"mcp-registry-admin mcp-servers-unrestricted/read mcp-servers-unrestricted/execute",
  "token_use":"access","token_kind":"user","iat":now,"exp":now+600}
h, p = b64(json.dumps({"alg":"HS256","typ":"JWT"}).encode()), b64(json.dumps(claims).encode())
tok = f"{h}.{p}.{b64(hmac.new(secret.encode(), f'{h}.{p}'.encode(), hashlib.sha256).digest())}"
req = urllib.request.Request("https://mcp.finddatatech.cloud/api/patch-keys",
    method="POST", data=json.dumps({"name":"<用途标签>"}).encode())
req.add_header("Authorization", f"Bearer {tok}")   # 无会话 cookie → CSRF 免检
req.add_header("Content-Type", "application/json")
print(json.loads(urllib.request.urlopen(req, timeout=20).read()))  # 明文只出现一次
PY
```

要点：自签 JWT 仅 10 分钟、只用于铸造这一次（密钥不出主机，用完即弃）；
**明文立即写入受限文件（0600）或目标 Secret，绝不进对话、日志与 git**。

### 2.2 备选：控制台 JWT（人工，不推荐常驻服务）

控制台 OAuth2 登录 → `POST /api/tokens/generate`：**默认 8h、上限 7d**，
到期即全线 401（历史上「MCP 挂了」的头号误报源）。仅用于临时排查，常驻
服务一律用 2.1 的 patch key。

### 2.3 台账与登记（两种通道共用）

台账登记（`~/finddata/secrets`）：服务名、username、key 形态与前缀、铸造
日期、用途、责任人、存放位置。**凭据明文永不入 git**。

## 3. 轮换流程

- **JWT（≤7d）**：到期前在控制台重铸 → 先把新 key 配到消费端（生态侧
  env）→ 打一次 wire 入口验证 → 控制台吊销旧 token。建议运维日历提醒
  （同 `CELL_REGISTRY_TOKEN` 重铸纪律）。
- **PAT（建议 90d 周期）**：铸新 key → 消费端切换 → 吊销旧 key；吊销前
  确认 `usage/overview` 已无旧 key 的在途调用。
- **username 不变时**，wire 集群侧 Secret **无需改动**——轨道判定按
  username 名单（`platform_identities`），不按 key 值；只有身份新增 /
  摘除才触发第 4 节回填。

## 4. 发放后回填 wire 集群（联动步骤，缺一步计量归属就错）

1. **追加 `platform_identities`**（Secret `fd-wire-env`，cheap 集群 fd-prod
   命名空间）：逗号分隔 username 名单，把新身份追加进去：

   ```bash
   kubectl --context cheap -n fd-prod create secret generic fd-wire-env \
     --from-literal=platform_identities='platform-agent,yizuo-market,yizuo-agent-01' \
     ...（其余 13 键原样重申，键清单见 deploy/README.md 键名表） \
     --dry-run=client -o yaml | kubectl apply -f -
   ```

   **名单不可留空**：空名单 = 一切调用判 customer 轨，壹座/萬星调用会错落
   客户账单（结算 SQL 硬过滤 `credential_track=customer`，错轨即错结算）。
2. **维护来源映射**（区分壹座 / 萬星 / 平台内部）：
   - `metering_source_map`（customer 轨 server → 来源，已在 Secret 键清单内）
     与本节无关，勿动。
   - `PLATFORM_IDENTITY_SOURCE_MAP`（platform 轨「身份前缀 → 来源」，
     api 侧已支持）按前缀通配维护
     `yizuo-*=yizuo,wanxing-*=wanxing,platform-agent=platform-internal`。
     **注意**：该 env 尚未接入 `deploy/k8s/fd-wire-api.yaml`（见
     deploy/README.md 键名表附注）——接入 manifest 前维持
     `platform-internal` 合桶，接入时同步 Secret 键与本表。
3. **滚动生效**：回填后重启 / 滚动 `fd-wire-api`（env 经 secretKeyRef 注入）。
4. **验证**：用新凭据打一次 wire 入口工具调用，然后
   `GET /api/admin/usage/overview?credential_track=platform`（或
   `GET /internal/metering/usage?credential_track=platform`，后者需
   `METERING_INGEST_TOKEN`）——应出现该 username，且 source 落在对应桶
   （映射接入前为 `platform-internal`）。
5. 台账登记回填完成（谁、何时、改了哪些键）。

## 5. 客户轨凭据（per-customer，计费到客户账单）

客户轨与平台轨**严格分轨**（ADR-0003：计量归属跟着凭据走，探索归平台、
物化归客户）。客户轨凭据 = 该客户的长命 key，注入 cell 的
`WIRE_CUSTOMER_KEY`——cell 内引擎的委托读经回环代理携带它，计量落
`credential_track=customer`，进客户账单。

### 5.1 开户 ceremony（每客户一次，2026-10-08 起一条命令）

前置（每客户一次性输入，cheap-1 `/opt/cell-manager/env.sh`）：

- `CELL_CUSTOMER_IDENTITY_MAP` 加一行 `"<customer_id>": {"username": ..., "sub2api_user_id": ...}`
  （sub2api 账户由 wire api 建户时返回 user_id，`name=wire-<customer_id>`）
- sub2api 充值：`POST /api/v1/admin/users/{id}/balance {"operation":"add","balance":N}`

然后两步（cell-manager，cheap-1）：

```bash
source /opt/cell-manager/env.sh
python -m cell_manager provision <customer_id>   # 建容器+卷+注册（条目自带 group-restricted 可见性）
python -m cell_manager grant <customer_id>       # cell 授权束：scope 文档（含发现面声明）+
                                                 # 条目可见性重申 + 预检映射（经 admin API 写
                                                 # sub2api_caller_map，下一请求即生效）
python -m cell_manager mint-key <customer_id>    # 客户 key：admin 代铸 API（归属/组快照精确），
                                                 # 明文仅本次 stdout —— 立即写入 0600 secret 文件
                                                 # 并加入 CELL_CUSTOMER_CREDENTIAL_MAP 后删除终端记录
python -m cell_manager revoke-key <customer_id> <key_id>   # 吊销（admin 旁路，幂等）
```

要点（均已自动化，不再是手工步骤）：

- 组快照 = `["wire-customers", "wire-cust-<customer_id>"]`（合成组不经 Logto，ADR 0005）；
  调用面/发现面由 per-customer scope 文档同时闭环，目录只见业务面+自己 cell
- 预检映射走 registry `PUT /api/sub2api-caller-map`（`CELL_PREFLIGHT_MAP_PATH`），
  **不再手改 auth-server 的 .env**；`PREFLIGHT_UNMAPPED=deny` 下漏配即时 402 CALLER_NOT_MAPPED
- 退租：`python -m cell_manager deprovision <customer_id>` 一条命令完成
  「吊 key → 删 scope → 注销条目 → 保留期」，幂等可重跑（cancel 后需重跑 grant）
- 旧五步手工 ceremony（Logto 建号→自签 JWT 铸 key→手改凭据 map→手改 .env→手工验证）
  已退役；2.1 节配方保留为无 admin 凭据时的应急通道

### 5.2 轮换 / 吊销

- 轮换：铸新 key → 更新 `CELL_CUSTOMER_CREDENTIAL_MAP` → **重建 cell 容器**
  （凭据在容器创建时注入，改 env 不生效；cell-manager 当前无热更新通道）
  → 吊销旧 key。
- 客户退租：先吊销 key（即时生效），再走 deprovision → purge 流程。

## 6. 平台服务凭据轮换 runbook（wgk- 形态）

1. 铸新 patch key（第 2.1 节配方，name 带日期便于台账区分）。
2. **wire 侧**：patch Secret `fd-wire-env` 的 `ai_platform_credential`
   → 滚动 `fd-wire-api`（ArgoCD 同步，约 3 分钟）。
3. **cell-manager 侧**：更新 `CELL_REGISTRY_TOKEN`（注册轨，若同身份）
   → 无需重启（进程内读取 env.sh）。
4. 验证：公网入口打一次 cell 工具调用（应 200）+ registry 审计侧出现新
   身份记录。
5. 吊销旧 key（`DELETE /api/patch-keys/{key_id}`），台账登记。

> 注意：轮换**不改 username** 时，wire 集群的 `platform_identities` 名单
> 无需改动（轨道判定按 username 名单，不按 key 值）。

## 7. cell 内双轨凭据（发放物的另一处消费，原第 5 节）

cell（企业隔离单元）内的回环代理 `wire_auth_proxy`（见
`cell-image/wire_auth_proxy.py`）按路径路由双轨：`/mcp` →
`WIRE_CUSTOMER_KEY`（客户轨）、`/mcp-platform` → `WIRE_PLATFORM_KEY`
（平台轨；未设 503 fail-closed）。这两个 key 由 cell-manager provision
时从 `CELL_CUSTOMER_CREDENTIAL_MAP` / `CELL_PLATFORM_CREDENTIAL` 注入——
**生产必须配置，未配置则对应轨道 fail-closed**（机制与注意见
`cell-manager/README.md`「配置」节）。平台轨 key 同样按本文第 2 节在
registry 铸造（platform- 前缀身份）。
