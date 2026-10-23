# Proposal: wire-artifact-marketplace（看板与研究报告的公开分享：只读看 + 可复制）

## Why

用户原话（2026-10-13）：「**规则引擎**和**公开分享的数据集和看板**是我们必须做
的一个工作」。对账现状：

| 件 | 状态 |
|---|---|
| 规则引擎 | ✅ 已上产（wire-quality-rules v1 + v2） |
| 数据集公开分享 | ✅ 已上产（wire-dataset-marketplace 四档可见性 + 市场） |
| **看板公开分享** | ⚠️ **只有点对点签名链接**（HMAC + TTL 7/30/90 天，发给谁谁能看）——**没有「发布到市场、被其他客户发现与使用」那一维** |

本 change 补齐第三件：**产物（看板 dashboard + 研究报告 research_report）也能
进入市场**，与数据集市场同构（四档可见性 + 检索 + 订阅 + 审批），并新增
**「使用」的两种语义**（用户裁决「这是个核心功能，都要做」）：

- **只读看（view）**：别人打开就是渲染好的交互页——筛选/查阅/下钻，面板取数
  走**发布方**的数据与口径、计量记发布方（像分享一份「活的分析报告」）；
- **可复制（copy）**：别人把该产物的 spec 复制到自己账号，成为自己的 draft
  产物可自由编辑（看板的面板绑定需重新绑定到自己可访问的数据集——
  **未订阅同源数据集时复制过来是未绑定面板，诚实标注，不假执行**）。

数据集侧的公开分享已按 wire-dataset-marketplace 的四档阶梯落地；本 change 把
同一阶梯**平移到产物维度**，并补上「复制」这一产物特有的使用语义。

## What Changes

- **数据层（fd-wire alembic `0036_artifact_marketplace`，AiBase）**：
  - `artifacts` 增四列：`visibility`（`private` 默认/`link`/`directed`/`market`）、
    `access_mode`（`open` 订阅即看/`approval` 需批准）、`published_at`、
    `published_version`（进入非 private 档位时刻的版本锚点）；
  - 新表 `artifact_market_subscriptions`（`customer_id` × `artifact_id` 唯一）、
    `artifact_share_grants`（定向授权名单）、`artifact_access_requests`
    （`pending|approved|rejected`，同数据集市场三表同构）；
  - **`allow_copy` 列**（bool，缺省 false）：发布方可选「允许他人复制此产物」。
- **可见性写入（同数据集市场纪律）**：单写入口 `set_visibility`——敏感度闸
  （产物无敏感度概念，改**红线闸**：含未开放数据面板的面板级提示，见下）、
  仅 owner/editor、幂等、append-only 留痕（`status_history` 追加
  `visibility` 事件，8 字段 before/after）、可随时撤回 private。
- **市场面（产物）**：
  - `GET /api/v1/market/artifacts`（检索：标题/描述/kind/标签筛选 + 分页；
    **SQL 层 `visibility='market'` 过滤**——默认私有，未开放永不出现）；
  - `GET /api/v1/market/artifacts/{id}`（详情：标题/描述/kind/版本/发布方/
    面板摘要——**不含任何数据值**）；
  - `POST/DELETE .../subscribe`（订阅即获得「只读看」权；approval 档走审批流）；
  - `POST .../copy`（**可复制语义**：要求 `allow_copy=true` 且订阅关系在；
    复制 spec 到 caller 账号成 draft；看板面板绑定重映射——面板绑定的
    dataset_id 若 caller 无访问权，该面板标 `unbound`（诚实标注，
    **不落假数据**）；返回新产物 id 与逐面板绑定报告）。
- **只读看（关键设计，见 design D2——初稿假设已被侦察修正）**：
  - 侦察证实看板渲染是**纯前端读 spec**（快照数据值内嵌在 spec）、零引擎
    调用。故「只读看」的真实语义 = **发布时刻的数据快照对订阅者可见**
    （Q1 裁决接受并**必须如实披露**）；零计量消耗；
  - **快照时间如实展示**（Q3；缺失显「未知」，不自动刷新）；
  - **「看板公开 ≠ 数据集开放」仍成立**：订阅者拿不到实时行数据（数据面
    仍按数据集市场闸），且**文案须说清两者差异**（D8 文案纪律）。
- **控制台**：
  - 产物页（ArtifactsPage）每个 published 产物加「可见性选择器」
    （四档 + 开放模式 + **允许复制**开关）——复用数据集侧
    `DatasetVisibilitySection` 的交互范式；
  - 市场页（MarketPage）加**产物页签**：`数据集 / 看板 / 报告` 三页签，
    产物卡片带「订阅」「复制到我的账号」按钮（后者仅 `allow_copy` 且已订阅时可见）。
- **文档**：DocsView 数据市场章节增产物开放说明（只读看 vs 可复制语义）；
  `docs/ROADMAP.md` 方向 B 勾注。

## 不做什么

- **不做产物收藏/点赞/排序推荐**（市场 v1 = 发现 + 订阅 + 复制，社交信号留 v2）；
- **不做跨客户 fork 溯源链**（复制记录留痕即有，fork 谱系留 v2）；
- **不做其他三种 kind**（composite_indicator / alert_rule / refresh_schedule
  是配置类产物，公开面价值低、风险面不同——本批只 dashboard + research_report，
  与用户裁决一致）；
- **不做公开免登录浏览**（市场 = 登录客户面，与数据集市场同纪律；免登录只有
  既有签名链接路径）；
- **不做报告渲染器**（Q2：research_report 的 spec 是 AI 自由 JSON、无渲染器；
  市场面用只读 JSON 查看器照 legacy 先例先通通道，报告渲染器另立 change）；
- **不做跨客户更新提示**（Q7：溯源指针留痕即止，订阅者不收到「原版已更新」）；
- **顺带修既有缺口（Q13）**：数据集「在架后被改敏感档不自动下架」——
  本批给 assets PATCH 加互锁（改敏感 → 自动撤回 + 留痕），两通道一起严。

## Impact

- 代码：`fd_wire_api/market/`（`artifact_*.py` 三件，与 dataset 版同构）、
  `fd_wire_api/artifacts/models.py`（四列 + allow_copy）、alembic 0036、
  `fd_wire_api/share/`（订阅者渲染路径扩展）、控制台两页改造、i18n；
- 兼容：纯新增列/表/端点；既有签名链接路径**零改动**（两条分享通道并存：
  链接=点对点临时、市场=公开可发现）；
- 规格：新能力 `artifact-marketplace`（ADDED）+ MODIFIED
  `artifact-lifecycle`（可见性维度）/ `dashboard-share`（渲染路扩订阅者）。
