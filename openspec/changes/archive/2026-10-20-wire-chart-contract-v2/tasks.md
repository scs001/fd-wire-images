## 1. 契约层：v2 解析与宽容归一

- [x] 1.1 在 `web/src/console/dashboard/spec.ts` 扩展面板类型枚举为类型谱两档（基础档 kpi/line/bar/table + 高级档十类），保持既有导出常量名与顺序兼容，验证：`npx tsc --noEmit` 通过，既有 `spec.test.ts` 全绿
- [x] 1.2 新增 `normalizeEncoding`（x 列 / y 列多选 / 系列列 / 大小列 / 轴类型 x 与 y / 轴名），逐字段宽容归一：非法轴类型回退 `category`、列名非字符串丢弃、空编码整体不落字段，验证：新增单测覆盖合法、部分非法、全非法三种输入，`vitest run spec` 全绿
- [x] 1.3 新增 `normalizePanes`（窗格数组、每格可选 encoding 与 overlays、高度权重正整数），非法窗格逐项丢弃；单窗格与不收 panes 不落字段，验证：单测断言单窗格输入不产生 panes 字段、坏窗格被丢弃
- [x] 1.4 新增 `normalizeOverlays`（markLine 线与 markArea 区间与 markPoint 点；每项含坐标/标签/线型），非法项逐项丢弃，验证：单测覆盖三型各自合法与非法项
- [x] 1.5 新增 raw_option 可序列化校验（深度遍历检出函数/正则/类实例/Date 等），不通过则该面板进 `badPanels` 且 reason 新增 `bad_raw_option`，验证：单测构造含函数值的 raw_option 断言被隔离且其余面板不受影响
- [x] 1.6 面板解析按字段存在性判定形态（有 encoding/panes/overlays/raw_option 即 v2），无 v2 字段走既有 v1 分支且产出对象形状与升级前逐字段一致，验证：对既有 v1 fixture 做深比较断言无差异

## 2. 渲染层：轴、编码与多窗格

- [x] 2.1 抽出 option 构建器：由 `encoding` 生成 xAxis/yAxis/series（含多 y 列、系列分组、大小列），未声明 encoding 时回退既有"第一列 x 其余列系列"分支，验证：单测对同一快照比对回退分支产出的 option 与升级前一致
- [x] 2.2 支持轴类型：category / value / time / log；time 轴在渲染层把快照首列字符串按 ISO·YYYY·YYYY-MM 解析为时间戳，解析失败点置 null（断线不错位），验证：单测覆盖等间隔、不等间隔、含非法日期三种序列的解析结果
- [x] 2.3 支持多窗格：N 个 grid 上下排列、每格一对轴、axisPointer link 串联、dataZoom 的 xAxisIndex 覆盖全部窗格；窗格高度按声明权重换算并设最小高度下限，低于下限降级为只渲染主窗格并标注，验证：单测断言两窗格时 grid 数与 axisPointer.link 形状；快照断言单窗格产出与单图面板一致
- [x] 2.4 支持叠加层：markLine/markArea/markPoint 写入对应窗格的 series，验证：单测断言叠加层落在正确窗格、无叠加层时 option 与升级前一致
- [x] 2.5 接入 raw_option：先按类型化字段生成 option，再与 raw_option 深合并（对象深合、数组整体替换、类型化字段优先），验证：单测覆盖"仅 raw"、"raw 与 encoding 共存时 encoding 生效"、"数组替换语义"三种情形
- [x] 2.6 图高可声明：面板 `height` 生效，未声明沿用 220 默认（v1 视觉零漂移），验证：单测断言未声明时 height 值与升级前相同；组件测试断言声明高度后容器样式生效
- [x] 2.7 接入交互：tooltip trigger axis + axisPointer cross、dataZoom inside（可选 slider）、图例开关，缩放平移只改视窗，验证：组件测试断言 option 含 axisPointer cross 与 dataZoom；手动缩放后快照与 spec 未变

## 3. 工具栏与图数切换

- [x] 3.1 `DashboardRenderer` 新增可选 `toolbar` prop（默认 false），为 true 时每个图表与表格面板渲染工具栏容器，验证：组件测试断言默认渲染与升级前 DOM 一致、toolbar=true 时出现工具栏
- [x] 3.2 实现全屏查看：固定定位覆盖层 + 原容器保留（不移动 DOM，仅改容器样式），退出恢复原布局，验证：组件测试点击全屏后容器带上覆盖层标记、退出后标记移除且 spec 未变
- [x] 3.3 实现图数切换：图表面板可切到既有表格渲染（同一 snapshot），切回无损；raw_option 面板按 series 名抽两列表，抽不出时显示不可表格化提示，验证：组件测试覆盖普通图表切换与 raw_option 可抽/不可抽两种
- [x] 3.4 实现单面板导出图片（本地生成，零新端点）与复制该面板数据，验证：组件测试断言导出未发起网络请求、复制内容与快照一致
- [x] 3.5 三个调用点显式传参：产物页 `DashboardPane` 传 `toolbar`，分享页 `ShareView` 不传，验证：断言分享页 DOM 无任何工具栏/全屏/导出入口

## 4. 编辑器扩面

- [x] 4.1 新增面板时类型选择按两档呈现（基础档/高级档），选中高级档类型生成对应骨架快照，验证：组件测试断言两个档位分组完整、选中高级档类型后面板落库
- [x] 4.2 图表面板详情区新增编码通道表单（x 列、y 列多选、x 轴类型、y 轴类型、轴名），已有 encoding 回填，验证：组件测试改轴类型并保存后 spec 含该 encoding、快照未被改写
- [x] 4.3 图表面板详情区新增叠加层表单（阈值线：轴值/标签/线型；区间：起止/标签），验证：组件测试增删一条阈值线后 spec 的 overlays 对应变化
- [x] 4.4 新增面板高度编辑项，验证：组件测试改高度后保存，新版本 spec 含该高度
- [x] 4.5 raw_option 面板详情区走只读结构查看（JSON 只读呈现）+ 标题/跨度/高度/来源编辑，不出现编码表单与查询绑定表单，验证：组件测试断言该面板无 encoding 表单与绑定段、可见只读结构
- [x] 4.6 类型互转与复制在 v2 面板上的语义：高级档互转保 snapshot 与 encoding、复制深拷贝 panes 与 overlays，验证：单测断言互转与复制后 v2 字段完整保留

## 5. 刷新侧对齐

- [x] 5.1 契约刷新回写后 panic 面板的 v2 字段完整保留（encoding/panes/overlays/height 逐字段不变），验证：后端测试构造含 v2 字段的 query 面板执行刷新，断言新版本仅 snapshot 变化
- [x] 5.2 raw_option 面板在契约路径与 AI 路径均记 kept 并标注原因，验证：后端测试覆盖两条路径，断言 raw_option 面板快照与配置零变化、响应 status 为 kept
- [x] 5.3 AI 刷新轮不抹平 v2 结构（指令已要求仅更新 snapshot），补一条断言测试锁住该不变量，验证：mock LLM 返回保留 v2 字段的 spec 时新版本 v2 字段与刷新前一致

## 6. AI 生成侧指令

- [x] 6.1 更新 `api/fd_wire_api/ai/tools.py` 中 `dashboard_spec_create` 的 spec 描述为 v2 表述：类型谱清单、encoding 写法（含轴类型）、panes 与 overlays 示例、raw_option 的使用边界与禁止函数值、时间序列必须声明时间轴，验证：单测断言描述包含各要素关键词，后端测试全绿
- [x] 6.2 验证生成侧约束：构造一段不合契约（raw_option 含函数）的 spec 走保存路径，断言被拒并如实反馈，验证：后端测试覆盖该用例

## 7. 周边回归与收口

- [x] 7.1 分享页回归：v1 与 v2 看板在分享页均正常渲染，且无工具栏、无全屏、无导出，验证：`vitest run ShareView` 全绿
- [x] 7.2 市场面回归：面板摘要仍只取 {id,type,title} 三字段，v2 字段不进入市场详情响应，验证：`vitest run MarketPage` 与后端市场测试全绿
- [x] 7.3 整板 CSV 导出适配 v2：普通面板与可表格化的 raw_option 面板照常导出，不可表格化的 raw_option 面板 section 标注不可导出且不中断整板，验证：`vitest run csv` 全绿，新增三类面板混排的导出断言
- [x] 7.4 v1 零漂移总闸：对既有 v1 fixture 快照断言渲染产出的 option 与升级前逐字段一致，验证：该断言在 CI 中通过
- [x] 7.5 双语键值一致性（新增文案的 zh/en 键齐备），验证：`vitest run i18n` 全绿
- [x] 7.6 全量前端与后端测试，验证：`vitest run` 与 `pytest` 无新增失败
- [x] 7.7 浏览器实测：打开一份 v2 看板（含时间轴、副图、阈值线、raw_option 面板）逐项验证轴排布、光标联动、缩放、全屏、图数切换、导出图片，验证：实测记录逐条通过
- [x] 7.8 跑 `openspec validate wire-chart-contract-v2 --strict`，验证：输出 valid
