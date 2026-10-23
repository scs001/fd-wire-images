# Tasks: wire-share-branding

## 1. web 品牌面

- [x] 1.1 ShareView 品牌段：顶栏双语品牌锁定（寻数·柏讯 / Wire，复用仓内
  品牌资产/字体惯例）+ 产品首页链接（href 走既有 wire 入口域名）；四态
  同一顶栏；页脚署名升级（就绪态带看板名与版本，非就绪态产品署名）
- [x] 1.2 og 元信息：就绪态运行时注入 og:title/og:description/og:site_name
  （卸载清理，与 noindex 注入同手法）；诚实注释 JS 预览器边界
- [x] 1.3 i18n 双语 key + 测试：四态品牌一致、og 注入与清理、页脚署名、
  noindex 不回归

## 2. 收口

- [x] 2.1 web vitest 全量 + tsc 全绿（与并行编辑器改动合流后）
