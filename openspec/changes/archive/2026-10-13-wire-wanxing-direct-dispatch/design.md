## design

冻结契约（并行实施依据——api/web/deploy 三方按此独立编码）。

### D1 A2A 客户端（新文件 `api/fd_wire_api/demands/wanxing.py`）

对齐 2026-10-12 实弹探针验证过的调用形状（新域；**绝不调**
`platform.finddatatech.cloud/api/wanxing/*` 旧 facade——双部署两份
SQLite，白名单/幂等互不相通）：

- 端点：`POST {WANXING_A2A_BASE}/api/wanxing/v1/a2a/{WANXING_AGENT_SLUG}`
- 头：`Authorization: Bearer <WANXING_CALLER_KEY>`、`Idempotency-Key:
  <派生键>`、`Content-Type: application/json`
- 体（JSON-RPC message/send）：
  `{"jsonrpc":"2.0","id":<rpc_id>,"method":"message/send","params":
  {"message":{"role":"user","kind":"message","messageId":<uuid4>,"parts":
  [{"kind":"text","text":<工单 YAML 全文>}]}}}`
- 回执解析：`result.parts[*].text` 拼接为 `reply_text`；`result` 缺席或
  形态不合 → 协议错。

env（fd-wire-api.yaml）：

| 变量 | 取值 | 形态 |
|---|---|---|
| `WANXING_A2A_BASE` | `https://wanxing.finddatatech.cloud` | 明文 |
| `WANXING_AGENT_SLUG` | `packs-kkcie7nlrhluo4likpnn0w-spider-heal` | 明文 |
| `WANXING_A2A_TIMEOUT` | `120`（秒；agent 生成为长调用） | 明文 |
| `WANXING_CALLER_KEY` | sk- 键值 | secretKeyRef fd-wire-env/wanxing_caller_key（**已存在**） |

接口（测试注入用工厂模式，对齐 scope_client 出口惯例）：

```python
class WanxingInvokeError(Exception):
    # .reason ∈ {"unauthorized","forbidden","unreachable","protocol"}|None
    # 401 INVALID_KEY→unauthorized；403 CALLER_NOT_AUTHORIZED→forbidden；
    # 网络/httpx 异常→unreachable；非 JSON/形状不合→protocol；其余 4xx/5xx 原文
class WanxingClient(Protocol):
    def send_message(self, text: str, *, idempotency_key: str,
                     rpc_id: str | None = None) -> dict:
        # → {"http_status": int, "message_id": str, "reply_text": str}
def get_wanxing_client() -> WanxingClient | None   # 键未配置→None（503 口径）
def set_wanxing_client_factory(f) / reset_wanxing_client_factory()
```

红线：键值绝不入日志/异常文案/事件——错误只带状态码与 wanxing 回传的
error 码。客户端不共享实例（per-request 建、用完 close，对齐
HttpScopeClient 惯例）。

### D2 调度 service（`orchestration.py` 增函数，双闸唯一实现）

`dispatch_wanxing_service(db, demand, *, slug_override=None) -> dict`：

1. **出单闸复用**：从 `export_workorder_service` 抽公共闸助手
   `_workorder_admission(demand, slug_override)`（状态门/无源 422/禁爬
   复检/slug 派生+三态探测，occupied→409）——export 路径改调助手，
   行为零漂移（既有测试全绿为证）；
2. 构单：`build_workorder(demand, slug=slug)`（同一纯映射=同一脱敏红线）；
3. 幂等键：`wire-{demand.code}-{sha256(ticket_yaml)[:16]}`（同单重发
   同键）；rpc_id=`wire-{demand.code}-{sha[:8]}`；
4. 客户端 None → 503（明示未配置，零留档）；`WanxingInvokeError` → 追加
   `wanxing_dispatch_failed` 事件（error/reason/sha256/ticket_yaml——
   全文随档供人工通道）+ 502（响应也带全文）；
5. 成功 → 追加 `wanxing_dispatched` 事件（ticket_path/ticket_yaml/sha256/
   slug/idempotency_key/message_id/http_status/reply_excerpt=前 500 字）
   并 commit；**不追加** `workorder_exported`（拉取队列零收录）。

响应形状（200）：
`{ticket_path, ticket_yaml, slug, sha256, delivered: true, queued_for_pull: false, a2a: {http_status, message_id, idempotency_key, reply_excerpt}}`

事件常量：`EVENT_WANXING_DISPATCHED = "wanxing_dispatched"`、
`EVENT_WANXING_DISPATCH_FAILED = "wanxing_dispatch_failed"`。

### D3 端点（双闸）

- `POST /api/v1/demands/{demand_id}/wanxing-dispatch`
  （`demands/routes.py`；`X-Internal-Token` 闸复用 `_require_internal`；
  body=`WorkorderIn` 复用——slug 可覆写）
- `POST /internal/admin/demands/{demand_id}/wanxing-dispatch`
  （`admin/routes.py`；admin 鉴权；照 `export_demand_workorder` 双闸
  惯例，`WorkorderExportError` → `status_code/detail`，502/503 如实透传）

### D4 web（DemandsPage「转生成工单」区旁）

- in_progress 单按钮「萬星直连」（testid `admin-demand-wanxing-dispatch`）
  → POST `/internal/admin/demands/{id}/wanxing-dispatch`；
- 成功面板（testid `admin-wanxing-result`）：`a2a.reply_excerpt` +
  message_id + 幂等键 + slug；失败面板（testid `admin-wanxing-error`）
  展示错误文案 + 工单全文折叠（人工推送降级指引，文案对齐既有
  undelivered 口径）；i18n 三语照既有 `admin.demands.workorder.*` 模式扩
  `admin.demands.wanxing.*`。

### D5 决策记录

- **失败留档**：dispatch 失败追加事件（与 scope 交付失败留档同哲学——
  时间线是审计事实流，不是成功流）；未配置（503）不留档（无投递事实
  可记，纯配置错误）。
- **不写 metering**：本通道是出站调用（wire 付费给萬星），与入站
  metering_records（客户用量）不同域；调用成本由 sub2api 侧 $1 限额
  硬顶，消费留档在 wanxing 侧记录，wire 不重复记账。
- **超时 120s**：message/send 同步回执；flash 模型常规工单处理实测
  秒级，120s 留足余量，env 可调。
- **队列互斥靠事件类型**：`pending_workorders` 只认 `workorder_exported`，
  直连事件天然不进队——不加互斥锁，不加去重（操作员显式选通道）。
