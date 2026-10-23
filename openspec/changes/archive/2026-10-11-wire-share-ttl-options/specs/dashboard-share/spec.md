## MODIFIED Requirements

### Requirement: 签名分享链接

published 且 kind=dashboard 的产物可生成签名分享链接：令牌为
HMAC-SHA256(artifact_id|version|exp)，**有效期可选 7/30/90 天（缺省 30 天，
非法值 422 列合法项）**，钉死生成时刻的版本（稳定快照分享，改版后重新生成）；
draft/archived/非 dashboard 不可生成；未配置 FD_WIRE_SHARE_SECRET 时分享整体
禁用（503 带原因）。

#### Scenario: 生成与打开
- **WHEN** 客户对 published 仪表盘生成分享链接，接收者未登录打开
- **THEN** 免登录渲染该版本的只读看板

#### Scenario: 版本钉死
- **WHEN** 分享后产物被编辑出新版本，旧链接打开
- **THEN** 仍呈现分享时刻版本的内容，不自动跟随新版本

#### Scenario: 有效期自选
- **WHEN** 客户选择 90 天生成链接
- **THEN** 返回的 expires_at 为 90 天后，令牌验签按该有效期

#### Scenario: 归档即失效
- **WHEN** 产物被归档后打开既有分享链接
- **THEN** 返回 404 的无效链接页

#### Scenario: 不可索引
- **WHEN** 检查分享页响应
- **THEN** 含 noindex 标记，不进入搜索引擎
