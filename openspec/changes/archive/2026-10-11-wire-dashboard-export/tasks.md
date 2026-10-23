# Tasks: wire-dashboard-export

- [x] 1.1 web `console/csv.ts`：toCsv（RFC4180/BOM/数值原样）+
  boardToCsv（逐面板 section；legacy 无面板→仅标题行）纯函数 + 单测
- [x] 1.2 DashboardPane 查看态「导出 CSV」按钮（blob 下载 {标题}.csv）+
  i18n；分享页/渲染器零改动断言（既有 ShareView 测试护住）
- [x] 1.3 测试：转义边界、section 结构、空板、下载触发、分享页无入口
- [x] 1.4 web vitest 全量 + tsc 全绿
