## Why

看板今天能表达的全是「数字的形状」：kpi / line / bar / table 四类基础面板加十类
高级图表，数据形状一律是行列矩阵。文本内容（新闻、公告、政策、研报条目）虽然
可以塞进 table 面板，但表格把标题、摘要、时间、来源压成同权重的单元格，图片列
只能显示一串 URL——信息层次丢失，图文内容在柏讯里没有可读的呈现形态。

而「图文信息流」恰是两条线共同的下一步：公开橱窗要往外做推广面，光有数字看板
撑不起内容感；方向 D 的个人信息工作台要处理「数百个信息源」，现有 source-packs
切的是指标目录（数值），文本信息源这一层还是空的。数据面其实已经够用——数据集
支持任意 TEXT 列、查询面支持 filters/sort/limit、刷新有确定性契约路径、分享与
橱窗复用同一套渲染器——缺的只是「一块能把文本列排成信息流的画布」。

本变更只补这块画布：在既有看板契约里新增一个 `list` 面板类型，不新建产物
kind、不开新的公开读边界、不引入图片托管。

## What Changes

- **面板类型谱新增 `list`**：在 kpi/line/bar/table 与十类高级图表之外新增列表
  面板；以 `encoding` 声明角色映射（`title` 必填，`summary`/`image`/`time`/
  `source`/`link` 可选），列名全部指向快照列。
- **卡片流渲染**：每行渲染为一张卡片（图片在上，标题、摘要、时间与来源在下），
  按快照行序呈现；图片缺失或加载失败时整块不占位，不出现破图图标。
- **图文安全底线**：图片与外链只接受 `http(s)`，`javascript:`/`data:` 一律不
  作为可加载源；标题与摘要按纯文本渲染（不产生 HTML 注入点）；外链带
  `noopener noreferrer`。
- **编辑器**：新增面板时 `list` 出现在类型选择里，带一个两列的骨架快照；面板
  详情区提供角色映射表单（选列），已声明时回填。
- **AI 生成侧**：`dashboard_spec_create` 指令补充 `list` 的写法与适用条件
  （数据含文本列时可用，必须显式声明角色，不得把长文塞进 table 假扮信息流）。
- **兼容面**：整板 CSV 导出按 list 面板快照行原样导出（与 table 同口径）；市场
  面板摘要仍是 `{id, type, title}` 三字段；橱窗与分享页零改动即可渲染（同一
  渲染器、同一快照来源）。

## Capabilities

### New Capabilities

（无——`list` 是既有看板契约的类型扩展，不新开能力面。）

### Modified Capabilities

- `dashboard-renderer`：面板类型谱新增 `list`；新增列表面板的编码通道契约、
  卡片流渲染规则、图文安全底线，以及与导出/摘要的兼容口径。

## Impact

- 契约与解析：`web/src/console/dashboard/spec.ts`（类型枚举、list 面板归一与
  宽容校验、角色映射的列名合法性）。
- 渲染：`web/src/console/dashboard/DashboardRenderer.tsx`（list 分支与卡片组件，
  图片/外链安全属性）；图表构建层 `chartOption.ts` 不参与（list 不走 echarts）。
- 编辑器：`web/src/console/dashboard/DashboardEditor.tsx`（类型选择、骨架快照、
  角色映射表单）。
- 导出与摘要：`web/src/console/csv.ts`（list 面板按快照导出）；市场摘要
  `api/fd_wire_api/market/artifact_browse.py` 的 `_panel_summaries` 无需改动。
- 生成侧：`api/fd_wire_api/ai/tools.py`（`dashboard_spec_create` 指令文案）。
- 复用面（同一渲染器，需回归）：`views/ShareView.tsx`、`views/ShowcaseView.tsx`、
  `console/DashboardPane.tsx`。
- 依赖：无新增第三方库；图片直连外链，不引入存储或代理。
- 非目标（明确不做）：RSS/Atom/JSON Feed 出口、图片上传与托管、客户端重排序、
  新的匿名读边界。
