# Tasks: wire-dataset-export

## 1. api 导出端点

- [x] 1.1 datasets/routes.py `GET /api/v1/datasets/{id}/export`：CSV 生成器
  （转义/BOM/空值/数值原样）；api 档 _effective_rows 全量；cell 档引擎
  query 分页循环（limit 1000/页，10 页上限）+ X-Export-Truncated；
  Content-Disposition；跨客户 404
- [x] 1.2 测试：api 档全量+修正口径、cell 档分页循环+截断头、转义边界
  （逗号/引号/换行/空值）、他企 404、未登录 401、Content-Disposition

## 2. web 导出入口

- [x] 2.2 DatasetDetail「导出 CSV」按钮：blob 下载 + 截断提示 + 失败如实；
  api client + i18n 双语
- [x] 2.3 测试：下载触发（含 Bearer）、截断提示、失败呈现

## 3. 收口

- [x] 3.1 api pytest 全量 + web vitest 全量 + tsc 全绿
- [x] 3.2 生产发布与实弹验证（导出 401 门/按钮 bundle 实证）
