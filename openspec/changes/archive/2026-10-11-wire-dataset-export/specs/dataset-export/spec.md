# dataset-export Specification（wire-dataset-export 新建）

## ADDED Requirements

### Requirement: CSV 导出端点

`GET /api/v1/datasets/{id}/export`（console 会话身份）导出有效视图 CSV：
api 档全量（免费档行数上限内）；cell 档引擎分页循环、总量上限 10,000 行、
超出 `X-Export-Truncated: true` 如实标注；UTF-8 带 BOM；列转义（引号/逗号/
换行）、空值空格、数值原样；文件名 = 数据集 key.csv；跨客户 404；导出与
预览/查询同有效视图口径（含修正）。

#### Scenario: 免费档全量导出
- **WHEN** 客户对含修正的 api 层数据集导出
- **THEN** CSV 呈现修正后有效视图全量行

#### Scenario: cell 档截断如实
- **WHEN** cell 档数据集行数超过 10,000
- **THEN** 导出前 10,000 行且响应头带截断标注，不静默丢行不谎报全量

#### Scenario: 企业隔离
- **WHEN** 客户请求他企数据集导出
- **THEN** 404，不泄露存在性

### Requirement: 导出入口

数据集详情提供「导出 CSV」按钮（双档同入口）：点击即下载（浏览器 blob），
截断时呈现截断提示；下载失败如实报错。

#### Scenario: 一键导出
- **WHEN** 客户在数据集详情点击导出
- **THEN** 浏览器下载 key.csv；截断场景附提示文案
