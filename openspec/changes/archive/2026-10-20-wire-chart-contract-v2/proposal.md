## Why

柏讯看板今天只能画四种面板（kpi / line / bar / table），且折线与柱状的 x 轴被
硬编码为类目轴（`DashboardRenderer.tsx` 的 `xAxis: { type: "category" }`）——
**时间轴在 v1 契约里根本无法表达**，而时间轴恰是行业看板与行情图表的第一需求。
配合固定的 220px 图高、无副图分区、无十字光标、无缩放、无阈值线，产出的看板
与客户对"专业数据软件"的预期有可见落差；同时 `finddata/dashboards/` 下已有五套
成品看板（宏观景气、六国对比、A股截面、区域格局、平安财务），它们使用完整
echarts option（time 轴、log 轴、markLine 周期分界），**因契约不兼容而无法进入
柏讯**——数据、图表、成品俱在，唯独中间管道不通。

改动动力来自同一处瓶颈：不放开契约，后续的公开橱窗、行业看板批量上架、AI 浮层
的"帮我改这张图"三件事都无从落地。

## What Changes

- 面板契约从 v1 升级为 **v2**，新增四类表达力：**面板类型谱扩展**（在既有 4 类
  之外引入 echarts 原生类型族：candlestick / scatter / pie / heatmap / gauge /
  radar / treemap / sankey / funnel / boxplot）、**显式编码通道**（以 `encoding`
  取代"第一列当 x 轴"的位置约定，支持 time/value/log 轴类型、多 y 轴、多系列
  角色）、**多窗格**（`panes` 副图共享时间轴，对应交易软件的主图+成交量+指标
  分区范式）、**叠加层**（`markLine` / `markArea` / `markPoint` 阈值线与事件
  标记）。
- 新增 **`raw_option` 逃生舱**：面板可携带完整 echarts option 直通渲染，绕过
  类型化字段。这是让存量成品看板零转换迁入柏讯的通道，也是 AI 生成任意图表的
  出口；类型化字段是它的可编辑子集，二者可共存（类型化字段优先、raw 兜底）。
- 渲染器升级：轴类型不再硬编码、图高可配置、多窗格与副图联动、十字光标
  （axisPointer cross）、区间缩放（dataZoom）、图例开关系列。
- 新增**图表工具栏**：全屏查看、导出图片、在"图 / 数据表"之间切换、复制面板
  数据。工具栏与既有整板 CSV 导出共存，分享页仍保持零导出红线。
- 编辑器随契约升级：类型选择按新类型谱展开、轴类型与编码通道可调、面板高度可
  调、`raw_option` 面板走只读 JSON 查看（不在 UI 里手写 option）。
- **兼容性**：v1 spec 零改动继续渲染（v1 是 v2 的子集，解析器按面板形态判定），
  存量产物、分享链接、市场摘要行为不变。
- 刷新契约同步：`query` 绑定面板在 v2 下仍走确定性契约刷新，刷新结果按 v2 数据
  形状回写；`raw_option` 面板不参与刷新（保留原快照，响应记 kept）。
- **BREAKING**（对生成侧而言）：AI 工具 `dashboard_spec_create` 的指令从"四类
  面板 + 第一列当 x 轴"改为 v2 形态；已生成的历史产物不受影响。

## Capabilities

### New Capabilities

（无——本变更全部落在既有看板能力的契约扩展上，不新开能力面。）

### Modified Capabilities

- `dashboard-renderer`：面板契约由 v1 扩展为 v2（类型谱 / 编码通道 / 多窗格 /
  叠加层 / raw_option 逃生舱）、渲染器轴与图高解耦、新增图表工具栏与图数切换、
  编辑器按新契约扩面、AI 生成侧指令升级。
- `dashboard-refresh`：确定性契约刷新在 v2 面板上的回写口径——带 `query` 的
  面板按 v2 数据形状刷新，`raw_option` 面板一律 kept 不动。

## Impact

- 契约与解析：`web/src/console/dashboard/spec.ts`（面板类型枚举、v2 规范化与
  宽容校验、v1↔v2 判定）、`web/src/console/dashboard/DashboardRenderer.tsx`
  （轴类型、图高、多窗格、工具栏）。
- 编辑器：`web/src/console/dashboard/DashboardEditor.tsx`（类型谱与编码表单）、
  `PanelQueryBinding.tsx`（试取回写按 v2 形状）。
- 周边复用面（均引用同一渲染器，需回归）：`views/ShareView.tsx`（公开分享页）、
  `console/DashboardPane.tsx`（查看态与工具栏挂点）、`console/csv.ts`（整板
  导出读面板数据）、`console/MarketPage.tsx`（面板摘要三字段）。
- 后端：`api/fd_wire_api/artifacts/refresh.py`（契约刷新按 v2 回写、raw_option
  面板 kept）；`validate_kind_spec` 对 dashboard 仍只走通用层（结构宽容在
  前端，零后端校验面变更）；`api/fd_wire_api/ai/tools.py`（生成侧指令文案）。
- 依赖：无新增第三方库——echarts 5.6 已在依赖内，全部新能力均为其原生特性。
- 迁移：`finddata/dashboards/*.spec.json` 五套成品转为 `raw_option` 面板的转换
  脚本属下一变更（`wire-showcase-market`），本变更只负责让契约容得下它们。
