# dashboard-share Delta（wire-rich-text-docs）

## MODIFIED Requirements

### Requirement: 签名分享链接

published 且 kind∈{dashboard, form, record-view, doc} 的产物MUST可生成签名
分享链接：令牌为 HMAC-SHA256(artifact_id|version|exp)，**有效期可选
7/30/90/0 天（缺省 30 天，非法值 422 列合法项；0=不设期·印刷物档——生成
面明示该语义与撤销途径）**，钉死生成时刻的版本（稳定快照分享，改版后
重新生成）；draft/archived/白名单外 kind 不可生成；dashboard 链接路径为
`/api/v1/share/dashboard/{id}`（只读渲染），form 链接路径为
`/api/v1/share/form/{id}`（只读渲染+公开提交，见 form-apps），record-view
链接路径为 `/api/v1/share/record-view/{id}`（只读渲染+按键实时取数，见
record-view），doc 链接路径为 `/api/v1/share/doc/{id}`（只读渲染，见
rich-text-docs）；未配置 FD_WIRE_SHARE_SECRET 时分享整体禁用（503 带原因）。

#### Scenario: 生成与打开
- **WHEN** 客户对 published 仪表盘生成分享链接，接收者未登录打开
- **THEN** 免登录渲染该版本的只读看板

#### Scenario: 表单链接
- **WHEN** 客户对 published 表单生成分享链接，接收者未登录打开
- **THEN** 免登录渲染该钉死版本的字段定义（零行数据），并可提交一条记录

#### Scenario: 溯源页链接
- **WHEN** 客户对 published 单记录公开页生成分享链接并选批次号出二维码
- **THEN** 扫码免登录打开该批次的实时取数页（令牌钉死版本）

#### Scenario: 文档链接
- **WHEN** 客户对 published 文档生成分享链接，接收者未登录打开
- **THEN** 免登录渲染该钉死版本的正文（无编辑入口，渲染安全底线适用）

#### Scenario: 版本钉死
- **WHEN** 分享后产物被编辑出新版本，旧链接打开
- **THEN** 仍呈现分享时刻版本的内容，不自动跟随新版本

#### Scenario: 有效期自选
- **WHEN** 客户选择 90 天生成链接
- **THEN** 返回的 expires_at 为 90 天后，令牌验签按该有效期

#### Scenario: 长效印刷物档
- **WHEN** 客户选择 0（不设期）生成链接
- **THEN** 令牌不携带过期时间（或等价语义），验签永不因过期拒绝；生成面
    呈现印刷物场景提示与「归档即失效」的撤销说明

#### Scenario: 归档即失效
- **WHEN** 产物被归档后打开既有分享链接
- **THEN** 返回 404 的无效链接页（含 0 档长效令牌同样失效）

#### Scenario: 不可索引
- **WHEN** 检查分享页响应
- **THEN** 含 noindex 标记，不进入搜索引擎
