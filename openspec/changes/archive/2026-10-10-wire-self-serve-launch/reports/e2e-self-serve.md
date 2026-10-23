# 4.7 自助注册端到端实弹（2026-10-09，生产真件）

## 全链时序（真实身份 doc-studio@finddatatech.com / sub=r80b1gx744mq）

1. **登录**：wire.finddatatech.cloud「使用 Logto 登录」→ Logto 授权码流 → 回调 `/console/callback` ✅（wire-console 应用首战）
2. **首登自动开户**（POST /api/console/session）：sub2api 代建（user 70）→ registry 铸 key（key_id e766db28…，组快照 [wire-cust-c1, wire-customers]）→ caller-map 写入 → logto_sub 映射落库（console_onboardings=done）✅
3. **key_once 一次性展示**：明文 wgk-KLzC…于控制台展示一次，DB 只存 key_id ✅
4. **200 调用**：MCP initialize 握手（serverInfo finddata 4.0.11）→ tools/list（10+ 工具）→ tools/call read/list_concepts 实调 ✅
5. **停用即断（key 侧）**：registry admin DELETE → 同 key 再握手 **401 Authentication required** ✅
6. **停用即断（额度侧）**：DB 置 suspended → `GET /internal/quota/70` → `included_lines=0, remaining_lines=0`；复活 → `1000/1000` 恢复 ✅（sha-b717c7d 的新行为）

## 过程中抓修的生产阻断缺陷（三修一接线）

| # | 缺陷 | 修复 |
|---|---|---|
| 1 | `fd-wire-env:sub2api_admin_key` 是 PLACEHOLDER、base 指错（lex-platform-v1 跨线挂账 #1） | 真钥+`https://router.finddatatech.cloud` 入 Secret（值经 shell 变量透传不回显） |
| 2 | wire sub2api 客户端整体旧契约（`/admin/api/users`+Bearer+amount_micro；真面=x-api-key+`/api/v1/admin/*`+envelope+email 建户+balance{balance,operation}） | 按 lex 修正版移植重写，测试同步（f226cae） |
| 3 | 铸 key 通道发 form+逗号串 groups（registry 422） | JSON+groups 列表（cell_manager 同径），HTTP 形状测试钉死（0ba2e85） |
| 4 | PATCH suspend 不吊 key、quota 不看 status（spec「停用即断」未接线） | best-effort 吊销 + 非 active 额度归零 + 2 测试（b717c7d） |

## 免费档余额门临态（如实记录）

registry 行数门（3.2，gate 默认 off 未灰度）上线前，免费客户 0 余额被既有余额门 402。E2E 以 1 微元入金过门（Idempotency-Key=e2e-topup-docstudio-1）。**3.3 灰度开后余额门对免超额档退位，临态消除。**

## 遗留（挂账）

- **registry 审计写入 2026-10-09 16:10 起停摆**（audit_events_default 无新事件，含 facet 线；容器 healthy、服务面正常）→ 计量 records 因此暂空（anomalies 14,516 全 missing_rows_returned）。registry 专属 session 处置。上午 add-customer-onboarding-automation 已实证「事件→入账 7 秒」，链路本身无罪。
- E2E key 已吊销（明文进过会话日志）；docstudio 为测试身份，新 key 由下次新用户首登或 admin 通道补铸。
- registry patch-keys 真端点=`https://mcp.finddatatech.cloud`（127.0.0.1:8080 是别的面，DELETE 200 但不生效——本报告实操踩坑）。
- tcr relay 新 tag 不自动同步：补单配方=cheap-1 `docker pull hkccr… && tag ccr… && push ccr…`（本窗用两次）。
