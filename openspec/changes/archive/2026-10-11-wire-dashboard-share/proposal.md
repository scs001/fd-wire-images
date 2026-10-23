# wire-dashboard-share

## Why

「把成果做成成熟的 web 直接分享给别人看」是用户明确的 D/B 方向诉求；dashboard
渲染器与 published 状态已就位，缺分享件。本 change 交付**签名分享链接**：published
仪表盘可生成对外只读链接（无登录），接收者打开即看板。

## What Changes

- **签名令牌（无表）**：`FD_WIRE_SHARE_SECRET`（未配置→分享功能整体禁用，端点
  503 带原因）；令牌=HMAC-SHA256(artifact_id|version|exp)，默认 30 天有效；
  **钉死版本**——链接固定生成时刻的版本（稳定快照分享；改版后重新生成链接）。
- **API**：`POST /api/v1/artifacts/{id}/share`（published 仪表盘→返回
  `{url, expires_at, version}`；非 published/非 dashboard 拒绝）；公开端点
  `GET /api/v1/share/dashboard/{artifact_id}?t=...`（验签+版本匹配+仍为
  published→返回 `{title, spec, version, shared_at}`；任一不满足 404）。
  归档即失效（published 检查兜底）；客户域数据零暴露（只有该产物自身）。
- **web 公开页**：`/#/share/dashboard/{id}?t=` 无需登录的只读 ShareView（复用
  DashboardRenderer；页脚「由柏讯分享」；`<meta name="robots" content="noindex">`
  ——既有红线：分享页不得入公开目录或搜索引擎；标题前缀「分享 · 」）。
- **产物页入口**：published dashboard 加「生成分享链接」按钮（展示链接+复制+
  到期时间；draft/archived 不出现）；非 dashboard kind 不出现。
- **AI 生成对齐 v1 契约**：dashboard_spec_create 工具描述内嵌 v1 结构示例
  （title/panels/type/span/snapshot 形状），使 AI 新生成的仪表盘默认可渲染可分享。

## Capabilities

### New Capabilities

- `dashboard-share`: 签名分享链接——版本钉死、published 校验、公开只读页
  noindex、生成/复制入口、未配置 secret 时诚实禁用。

### Modified Capabilities

- `dashboard-renderer`: 公开 ShareView 复用渲染器（只读态，无编辑入口）。
- `artifact-lifecycle`: published 仪表盘提供分享链接生成（draft/archived 不提供）。

## Impact

- **fd-wire/api**：share 模块（HMAC 工具+两个端点+FD_WIRE_SHARE_SECRET 配置）、
  ai/tools.py 工具描述、pytest。
- **fd-wire/web**：App 路由+ShareView、ArtifactsPage 入口、i18n、组件测试。
- **运维（本 change 内完成）**：fd-wire-env secret 加 share_secret 键 + 清单
  env FD_WIRE_SHARE_SECRET 注入。
- **红线**：分享页 noindex；公开端点只暴露单产物内容不含客户名单/其他产物。
