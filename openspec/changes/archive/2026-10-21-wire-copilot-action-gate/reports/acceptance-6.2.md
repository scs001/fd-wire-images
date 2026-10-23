# 6.2 真机验收报告（生产）

> 2026-10-21 · fd-wire 生产 `wire.finddatatech.cloud` · api `sha-22da877`（源真 `171b2e8`）
> 账号：wire-tester1@findmail.com（free）/ wire-tester2（basic）/ wire-tester3（standard）
> 方式：浏览器真登录（Logto PKCE）取 **真实 ID token**，随后直打生产 API 跑全链；
> 脚本与逐条结果见同目录 `acceptance-6.2-api.py` / `acceptance-6.2-api.json`。

## 结论

动作闸的提议-确认-执行-审计在真账号、真库、真档位上成立；两处验收中发现的
缺陷已当场修复并重新出片复验：

| # | 缺陷（验收抓到） | 违反的 spec 场景 | 修复 |
|---|---|---|---|
| 1 | `wire.artifact.promote` 只登记 `materialized_artifacts`，不建 `artifacts` 行 → 产物页为空，发布/分享链断 | 「转正后出现在产物页」 | 执行体改为 `import_from_draft`（建档）+ `promote_draft`（物化）双写；二次转正先查物化行，如实失败且不落第二份产物（48f957a） |
| 2 | 分享链接（含 `?t=<令牌>`）被写进提议/审计持久记录 | 「审计不含密钥」 | 持久化前脱敏 `?t=<token>`/`wgk-`/`Bearer`；approve 即时响应仍带完整链接供当场复制一次（171b2e8） |

## free 档（wire-tester1）

| 检查 | 结果 |
|---|---|
| 建会话 | 201 |
| 问答轨（`/ai/answer`） | **00:22 实测 200，23.7s，返回真实回答**；01:39 复测遇上游 token 网关 403/503，端点如实 502（见「外部阻塞」） |
| 读数据集/产物/需求单 | 200 |
| 构建类提议被拦 | 422「免费档（api 层存储），构建类动作需要开通数据处理引擎（cell）」——不落 pending |
| 写提议（需求单） | 201 `pending_confirm`，`missing_fields=[source_url, public_claim]`（合规字段留待卡上填） |
| 拒绝提议 | 200 `rejected`，底层未执行 |
| 审计检索 | 200，可按会话取回；无凭据明文 |

## standard 档（wire-tester3）完整链

| 步骤 | 结果 |
|---|---|
| 描述看板 → 草稿提议 | 201 `pending_confirm` / `tier=inline`，影响面文案随卡 |
| 卡上确认 → 执行 | 200 `executed`：「已创建仪表盘草稿「验收看板 …」（草稿 #3）」 |
| 草稿区可见 | 200，会话草稿列表命中该草稿（可预览） |
| 转正提议 → 确认 | 200 `executed`：「已转正为正式产物 #23「验收看板 …」（物化 #3），产物页可见」 |
| 产物页可见 | 200，`/v1/artifacts` 新增行 #23 |
| 发布 | 200 `executed`：v1，「队内可见」 |
| 分享提议 | 201 `pending_approval` / `tier=async`（未立即执行） |
| 审批前状态 | `pending_approval` 保持不变 |
| owner 批准 | 200 `executed`，返回分享链接 |
| 分享链接匿名访问 | 200（无 Authorization） |
| 审计/回放 | 4 条 `executed`；分享摘要落库为 `?t=***&v=1`，无令牌 |

## viewer 拒写（wire-tester1 临时作为 wire-tester3 队 viewer）

方法：生产库临时插入一行 `customer_members(8, kgrabrbuiloj, viewer)`，`POST /console/team/switch {customer_id:8}`
把 tester1 的活跃队切到 tester3 队（真实解析路径），验完即时删除成员行与指针行并切回自有队。

| 检查 | 结果 |
|---|---|
| viewer 读同队提议 | 200（读不受限） |
| viewer 确认写操作 | **403**「团队成员权限不足（viewer 只读），不能确认写操作」，提议状态仍 `pending_confirm` |
| viewer 发起写提议 | **422**「团队成员权限不足（viewer 只读）——写操作需由 owner 或 editor 发起」 |
| 清理 | 成员行 0、指针行 0，tester1 活跃队回到 6，读数据集 200 |

## 审计台账（生产库 `ai_action_audit`）

按团队 `wire-tester3@findmail.com` 检索：

```
wire.artifact.draft.create   ok  3
wire.artifact.promote        ok  3
wire.artifact.publish        ok  2
wire.artifact.share.create   ok  2
```

列集合 `id, proposal_id, customer_id, action_id, result, error_text, duration_ms, created_at`
——既可按团队/动作/时间检索，也没有任何凭据列；分享令牌在提议摘要里为 `t=***`。

## 外部阻塞（非本 change 引入）

`/ai/answer` 复测返回 502，原因链：token 网关 `chat/completions` → 上游
`403 Upstream access forbidden`（网关对配置模型 `TOKEN_GATEWAY_MODEL=deepseek-v4.1-flash`
的上游鉴权失败；网关 `/models` 当前列出的可用模型不含该模型）。fd-wire 侧行为正确：
不伪造回答、502 带如实原因。00:22 同一部署曾真实答出，故问答轨代码路径已实证；
待网关上游恢复或改配模型后应补一次回归。

## 未覆盖

- 浏览器 UI 的逐点击走查：本轮 LLM 上游不可用，副驾「描述→卡」在 UI 内需要模型；
  组件层由 `AssistantPanel.test.tsx`（含转正提议）与 `ArtifactsPage.test.tsx`
  （事件刷新）覆盖。
