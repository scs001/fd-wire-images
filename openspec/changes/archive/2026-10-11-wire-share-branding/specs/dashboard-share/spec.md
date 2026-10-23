# dashboard-share 变更（wire-share-branding）

## ADDED Requirements

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

## MODIFIED Requirements

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
