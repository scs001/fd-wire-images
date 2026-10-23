# dashboard-share Specification

## Purpose
定义 published 仪表盘的对外分享：签名链接（版本钉死+有效期）、公开只读渲染页
（免登录、noindex）、生成入口与诚实禁用（未配置 secret）——使客户能把看板成果
直接分享给任何人看，同时守住「分享页不入公开目录/搜索引擎、公开面零客户数据
泄露」红线。

## Requirements

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

### Requirement: 公开只读页红线

公开分享页复用看板渲染器（只读，无编辑入口），带 noindex meta 与品牌署名
（见「分享页品牌面」）；公开端点仅暴露该产物自身内容；令牌无效/过期/产物
非 published 一律 404（错误形态一致，不泄露差异原因）。

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

### Requirement: 分享页品牌面

公开分享页四态（加载/就绪/无效/未开通）统一品牌面：顶栏为双语品牌锁定
（寻数·柏讯 / Wire）并链到产品首页；页脚就绪态为「看板名 · 由 寻数·柏讯
提供 · 版本 v{n}」，非就绪态带产品署名；就绪态运行时注入 og:title（看板
名）/ og:description / og:site_name 链接预览元信息（对不执行 JS 的爬虫
无效为已知如实边界）；noindex 与 404 统一口径原样不动。

#### Scenario: 品牌一致
- **WHEN** 打开任一状态的分享页
- **THEN** 顶栏呈现同一品牌锁定与产品首页链接

#### Scenario: 链接预览元信息
- **WHEN** 分享页就绪
- **THEN** 页头含 og:title=看板名、og:site_name=柏讯品牌，且 noindex 仍在
