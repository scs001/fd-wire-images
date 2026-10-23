# Tasks — wire-console-shell

## 1. 侧栏分组

- [x] 1.1 `CONSOLE_TABS` 改为分组结构（四组，组内保持既有顺序）
- [x] 1.2 `ConsoleLayout` 渲染分组标题 + 组内页签（保留既有 data-testid）
- [x] 1.3 当前页签高亮与 `aria-current` 在新结构下保持准确
- [x] 1.4 i18n：四组标题（概览/数据/构建/运营）中英文案
- [x] 1.5 窄屏：侧栏横向化，内容区不被挤压

## 2. 宽度契约

- [x] 2.1 内容区由外壳统一给出宽度基线
- [x] 2.2 清理页内整页级 `max-w-*` 自限（保留段落级约束）
- [x] 2.3 目视确认多页签左边缘对齐一致

## 3. 验证

- [x] 3.1 前端测试：四组渲染、页签齐全、aria-current、语言切换
- [x] 3.2 既有 966 项测试全绿（分组不破坏路由与权限断言）
- [x] 3.3 `tsc --noEmit` + `npm run build` 通过
- [x] 3.4 `openspec validate wire-console-shell --strict` 通过
- [x] 3.5 真机目视：分组观感、窄屏、跨页签对齐
