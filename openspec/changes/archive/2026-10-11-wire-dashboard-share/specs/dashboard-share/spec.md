## Purpose

定义 published 仪表盘的对外分享：签名链接（版本钉死+有效期）、公开只读渲染页
（免登录、noindex）、生成入口与诚实禁用（未配置 secret）——使客户能把看板成果
直接分享给任何人看，同时守住「分享页不入公开目录/搜索引擎、公开面零客户数据
泄露」红线。

## ADDED Requirements

### Requirement: 签名分享链接

published 且 kind=dashboard 的产物可生成签名分享链接：令牌为
HMAC-SHA256(artifact_id|version|exp)，默认 30 天有效，**钉死生成时刻的版本**
（稳定快照分享，改版后重新生成）；draft/archived/非 dashboard 不可生成；
未配置 FD_WIRE_SHARE_SECRET 时分享整体禁用（503 带原因）。

#### Scenario: 生成与打开
- **WHEN** 客户对 published 仪表盘生成分享链接，接收者未登录打开
- **THEN** 免登录渲染该版本的只读看板

#### Scenario: 版本钉死
- **WHEN** 分享后产物被编辑出新版本，旧链接打开
- **THEN** 仍呈现分享时刻版本的内容，不自动跟随新版本

### Requirement: 公开只读页红线

公开分享页复用看板渲染器（只读，无编辑入口），带 noindex meta 与「由柏讯分享」
标识；公开端点仅暴露该产物自身内容；令牌无效/过期/产物非 published 一律 404
（错误形态一致，不泄露差异原因）。

#### Scenario: 归档即失效
- **WHEN** 产物被归档后打开既有分享链接
- **THEN** 返回 404 的无效链接页

#### Scenario: 不可索引
- **WHEN** 检查分享页响应
- **THEN** 含 noindex 标记，不进入搜索引擎

### Requirement: AI 生成对齐 v1 契约

AI 仪表盘生成工具的描述内嵌 v1 结构示例（标题/面板/类型/跨度/快照形状），
新生成的仪表盘默认符合可渲染可分享的 v1 结构。

#### Scenario: 新生成即可渲染
- **WHEN** AI 按工具描述生成仪表盘草稿
- **THEN** 产出的 spec 结构可被 v1 校验解析为面板化看板
