# Proposal: wire-dashboard-export — 看板整板 CSV 导出

## Why

数据集侧导出已落（wire-dataset-export），看板侧没有——客户把看板数据带给
下游（对账/汇报/二次加工）只能截图。面板快照本就内嵌在产物 spec 里（契约
刷新上限 1000 行/面板），客户端本地即可生成 CSV，零请求零新端点。

## What Changes

- **CSV 工具（web）**：`csv.ts` 纯函数——RFC4180 转义（引号/逗号/换行）、
  UTF-8 BOM、数值原样；整板合成 = 逐面板 section（面板标题注释行 + 列头 +
  数据行 + 空行分隔），legacy spec 无面板 → 空导出如实（只有标题行）。
- **查看态导出入口**：DashboardPane 工具区「导出 CSV」——整板单文件下载
  （`{产物标题}.csv`），数据来自面板快照本地生成；仅控制台查看态提供，
  **分享页与渲染器本体零导出入口**（公开只读红线不动）。
- 无面板/全空快照照常导出（如实空文件结构），不报错。

## Impact

- **specs**：dashboard-renderer ADDED「看板导出」。
- **web**：`console/csv.ts` 新工具 + DashboardPane 按钮 + i18n。
- **兼容**：纯客户端新增；分享页零改动。

## Non-Goals

- 服务端导出端点（快照在 spec 内，无需服务端）。
- 分享页导出（公开只读红线：无任何动作入口）。
- xlsx/图表图片导出。
