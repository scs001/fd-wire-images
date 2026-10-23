# 6.3 回滚演练报告（生产）

> 2026-10-21 01:44–02:05 CST · fd-wire 生产（cheap 集群 fd-prod）
> 演练目标：回滚 api/web 镜像后回到「问/做两态」现状；`ai_action_proposals` /
> `ai_action_audit` 两表与 `/api/v1/ai/actions` 新端点留存无消费者、无副作用。

## 结果

**通过。** 回滚期间旧版行为完整恢复，新端点 404；两表与迁移版本原样保留，
回滚与前滚前后行数完全不变；前滚后新端点与新 UI 恢复。

| 阶段 | api tag | web tag | 验证 |
|---|---|---|---|
| 回滚 | `sha-c50fe51`（变更前） | `sha-ed4182c`（由变更前源码 `95f7bd6` 现打） | `GET /api/v1/ai/actions` → **404**，`POST …/propose` → **404**；`GET /v1/ai/conversations` → 200；web 免费档页面显示旧「问答模式」徽章、无动作卡/草稿区；旧 bundle 含 `assistant-mode-switch`、不含「动作卡」文案 |
| 前滚 | `sha-22da877` | `sha-c5b5cf4` | 新端点未鉴权 → **401**（路由存在）；web 恢复新版单一副驾（「免费档」徽章 + 待确认动作卡文案） |

## 两表留存与无副作用（基线 vs 回滚 vs 前滚）

| 表 | 回滚前 | 回滚中 | 前滚后 |
|---|---|---|---|
| `ai_action_proposals` | 13 | 13 | 13 |
| `ai_action_audit` | 10 | 10 | 10 |
| `artifacts` | 22 | 22 | 22 |
| `materialized_artifacts` | 3 | 3 | 3 |
| `alembic_version` | `0039_ai_action_gate` | 同 | 同 |

旧代码不认识两表、不注册新端点：回滚窗口内无写入、无报错，验证了
「迁移先行 + 存量无消费者」的发布纪律。

## 演练中发现的发布面事实（已修正标签口径）

- 初始选用 `sha-7548a16` 作回滚目标不成立：其 api 是变更前代码（404 正常），
  但 **web 镜像实际已含新版 UI**——历史上「镜像 tag = 快照仓 sha」与源码真 sha
  不同源，不能凭 tag 推定内容。
- 更换为变更前部署标签 `sha-c50fe51` 后，web 仍渲染新版 UI；进一步核对发现
  该标签的构建源同样晚于变更前。最终**从变更前提交 `95f7bd6` 现打 web 镜像
  `sha-ed4182c`** 作为演练目标，并在快照仓内核对 `assistant-mode-switch`
  存在后才开滚。
- 页面走查须**强制重载**：SPA 的 hash 路由切换不会重新下载 JS，回滚后
  首次快照仍是内存里的新版界面，容易误判。

## 遗留

- 演练用的 `fd-wire/web:sha-ed4182c` 标签留在 TCR（约占一个 tag 配额）。
- 回滚窗口约 8 分钟（两次 ArgoCD selfHeal 滚动），期间生产为变更前版本，
  未影响存量客户读写。
