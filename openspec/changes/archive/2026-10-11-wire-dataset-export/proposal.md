# Proposal: wire-dataset-export — 数据集 CSV 导出

## Why

客户数据进得来（上传/连接器/派生）、看得见（预览/编辑/看板）、刷得动（契约/
调度），但**带不走**——没有导出。数据可带走是信任与实用的基本件：对账、
二次加工、交付给下游工具都要 CSV。导出走有效视图（含修正），与预览/查询
同口径。

## What Changes

- **导出端点**：`GET /api/v1/datasets/{id}/export`（console 身份）——
  CSV 流（UTF-8 带 BOM，Excel 直开）；api 档全量有效视图导出（免费档行数
  上限内）；cell 档引擎 query 分页循环导出，总量上限 10,000 行，超出响应头
  `X-Export-Truncated: true` 如实标注；列转义（引号/逗号/换行）、空值空格、
  数值原样；`Content-Disposition` 文件名 = 数据集 key.csv；跨客户 404。
- **导出入口**：数据集详情「导出 CSV」按钮（两档同入口），截断时提示
  「已截断至前 10,000 行」。

## Impact

- **specs**：新能力 `dataset-export`（导出端点 + 导出入口两要求）。
- **api**：datasets/routes.py 新端点（api 档复用 _effective_rows；cell 档
  引擎分页循环）。
- **web**：DatasetDetail 按钮 + blob 下载 + i18n。
- **兼容**：纯新增端点与入口。

## Non-Goals

- Excel/xlsx 双格式（CSV+BOM 覆盖 Excel 场景；xlsx 另立项）。
- 看板产物导出（面板快照导出随后续轮）。
- 增量/异步导出（10k 行内同步即可；超限如实截断）。
