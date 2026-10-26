# dashboard-renderer Specification

## Purpose
定义仪表盘产物的 v1 契约与控制台渲染/编辑：dashboard spec 从自由 JSON 升级为
可渲染结构（面板×快照数据），客户在产物页直接看见仪表盘并进行基础编辑（增删
面板/改标题/调布局），保存复用产物版本链——使 AI 生成的仪表盘从"JSON 文件"
变成"看得见的看板"，为方向 B（自定义看板与分享）铺第一块可视地基。

## Requirements

### Requirement: 仪表盘 spec v1 契约

dashboard 产物 spec 采用 v1 结构：标题 + 面板数组；每个面板含类型
（kpi/line/bar/table）、标题、栅格跨度、来源标注与**内嵌快照数据**
（columns+rows），可选携带**查询契约引用**（query，见「面板查询契约」）；
前端提供宽容校验——结构不合法的面板跳过并标注，不拒绝
整个看板；无 v1 结构的旧自由 JSON 显示为旧版格式只读视图。

#### Scenario: 合法看板渲染
- **WHEN** 打开含 kpi/line/bar/table 四类面板的 dashboard 产物
- **THEN** 四类面板按栅格布局渲染，数据来自面板快照

#### Scenario: 坏面板隔离
- **WHEN** 看板中某面板 snapshot 形状不合法
- **THEN** 该面板显示问题标注，其余面板正常渲染

### Requirement: 快照渲染

渲染以面板内嵌快照为数据源：kpi 呈现大数卡（值/单位/副标题），line/bar 以
echarts 渲染（未声明编码时 x/y 依快照列推断），table 呈现列表（长表滚动）；
空快照如实显示无数据占位；渲染器标注快照口径（实时刷新属后续物化能力）。

面板高度 SHALL 可声明：未声明时沿用既有固定高度（保持 v1 视觉零漂移）；
声明后按声明值渲染，并随栅格跨度自适应容器宽度。

渲染 SHALL 提供十字光标（横向类目/时间对齐）与区间缩放（内嵌滚轮缩放 +
可选的缩放滑块），缩放与平移只改变视窗不改变数据。

#### Scenario: 空数据诚实呈现
- **WHEN** 面板快照为空
- **THEN** 显示无数据占位而非空白或报错

#### Scenario: 声明高度生效
- **WHEN** 面板声明较高的高度值
- **THEN** 该面板按声明高度渲染，同板未声明高度的面板保持默认高度

#### Scenario: 缩放不改数据
- **WHEN** 客户在长序列图表上缩放并平移视窗
- **THEN** 视窗变化，悬浮读数与数据表内容不变，spec 未被改写

### Requirement: 基础编辑器

客户可对面板做增/删/改标题/移序/调跨度/调高度操作，kpi 面板可编辑值/单位/
副标题；编辑实时预览；保存走既有产物 PUT 端点产生新版本；未保存离开有提示。
扩展面：**图表类型互转**（line↔bar↔table 保 snapshot 零数据损失；kpi 与图表类
互转需确认——kpi 只取首值有信息取舍）、**面板复制**（新 id+副本标题+深拷贝）、
**source 与 config 编辑**（source 文本提示"填指标 code/数据集名，刷新依赖此标
注"）、未保存变更计数展示。

v2 附加：新增面板时 SHALL 可从完整类型谱选择（见「仪表盘 spec v2 契约」）；
图表面板 SHALL 可编辑编码通道（x 列、y 列多选、轴类型、轴名）与叠加层声明；
`raw_option` 面板 SHALL 仅提供只读结构查看（标题/跨度/高度/来源仍可编辑），
且其数据来源 SHALL 以 `raw_option` 自含数据为准、不提供查询绑定表单。

#### Scenario: 编辑即版本
- **WHEN** 客户编辑面板标题并保存
- **THEN** 产生新版本号，重新打开看到新标题，历史版本仍可查

#### Scenario: 类型互转保数据
- **WHEN** 客户把折线面板转为表格
- **THEN** snapshot 原样保留，表格呈现同一数据，保存后新版本生效

#### Scenario: 面板复制
- **WHEN** 客户复制某面板
- **THEN** 出现新面板（副本标题、独立 id、相同数据），可独立编辑

#### Scenario: 编码通道编辑
- **WHEN** 客户把某面板的 x 轴类型从类目轴改为时间轴并保存
- **THEN** 新版本按时间轴渲染，快照数据未被改写

#### Scenario: raw_option 面板只读结构
- **WHEN** 客户展开一个 raw_option 面板的详情区
- **THEN** 可见配置结构只读查看，出现标题/跨度/高度/来源编辑项，
  不出现编码通道或查询绑定表单

### Requirement: 产物页入口

产物页对 dashboard kind 提供「查看看板」入口（渲染态）并可切换编辑态；其他
kind 的产物页行为不变。

#### Scenario: 入口按 kind 分发
- **WHEN** 客户在产物页打开 dashboard 产物
- **THEN** 呈现看板视图入口；composite_indicator 等其他 kind 不出现该入口

### Requirement: 面板查询契约

面板新增可选 `query` 引用（与数据面 query 端点入参同形同界）：
`{dataset_id, filters?, sort?, limit?, columns?}`——filters 算子集合
eq/in/gt/gte/lt/lte/contains/startswith、sort 方向 asc/desc、limit 1..1000、
columns 投影列序即快照列序。前端宽容归一：非法 query 整体丢弃（面板仍按
快照渲染，不受影响）；编辑器增删/移序/复制/类型互转一律保留 query 不抹掉，
面板详情区显示「已绑定查询」徽标（含 dataset_id）；渲染数据源仍是快照
（query 是刷新契约，不是渲染输入）。

#### Scenario: 契约面板渲染
- **WHEN** 打开带 query 引用与快照的面板
- **THEN** 渲染使用快照数据（契约不参与渲染），面板显示已绑定查询徽标

#### Scenario: 非法契约宽容降级
- **WHEN** 面板 query 形状不合法（如算子不在白名单、dataset_id 缺失）
- **THEN** 该 query 被丢弃，面板按快照正常渲染，看板不整板失败

#### Scenario: 编辑保留契约
- **WHEN** 客户对带 query 的面板改标题/调跨度/复制/类型互转后保存
- **THEN** 新版本面板的 query 原样保留

### Requirement: AI 生成侧契约产出

`dashboard_spec_create` 工具指令 SHALL 要求：面板数据取自数据集查询工具时，
面板必须同时携带 `query` 引用（所用 dataset_id 与查询形参如实落盘）；kpi
面板取单值采用 rows 模式约定（sort 到最近一行 + limit 1 + columns 投影到值列）；
`source` 仍作展示标注保留。

指令 SHALL 按 v2 契约表述：给出类型谱、编码通道（含轴类型）与多窗格的写法
示例；明确 `raw_option` 只用于类型化字段无法表达的图形，且不得携带函数或
不可序列化值；明确时间序列必须声明时间轴而非类目轴。

#### Scenario: 生成即带契约
- **WHEN** AI 经数据集查询工具取数生成看板面板
- **THEN** 该面板 spec 同时含 query 引用与展示用 source

#### Scenario: 生成时间序列用时间轴
- **WHEN** AI 为跨年份指标生成折线面板
- **THEN** 产出的面板声明时间轴编码通道，而非沿用类目轴位置约定

#### Scenario: 生成不得夹带函数
- **WHEN** AI 尝试在 raw_option 中写入函数型格式化器
- **THEN** 该面板被判为不合契约并如实反馈，不落库

### Requirement: 契约表单化绑定

编辑器面板详情区提供「绑定查询」段（未绑定的面板显示未绑定态）：数据集
选择（本企业数据集列表，呈现 tier 与行数）、投影列勾选（按数据集列序）、
过滤行（列 + 算子白名单 + 值）、排序（列 + asc/desc）、limit（1..1000）；
「试取数据」把试取结果写入面板本地快照（预览即所得，不自动保存）；「解绑」
清除面板 query；kpi 面板给「最近一期 + limit 1 + 投影值列」约定提示；保存
走既有版本链（query 随面板落盘）；已有 query 的面板表单回填原值。

#### Scenario: 人工绑定即契约
- **WHEN** 客户在编辑器选数据集、勾两列、加一条过滤并保存
- **THEN** 新版本面板 query 含该 dataset_id/列投影/过滤，刷新徽标仍呈现

#### Scenario: 试取预览
- **WHEN** 客户填好绑定条件点「试取数据」
- **THEN** 面板本地快照更新为试取结果（预览呈现），未保存离开仍提示

#### Scenario: 解绑
- **WHEN** 客户对已绑定面板点「解绑」并保存
- **THEN** 新版本面板不再携带 query，后续刷新按无契约面板处理

### Requirement: 看板导出

控制台查看态提供「导出 CSV」：整板单文件（逐面板 section=标题注释行 +
列头 + 数据行 + 空行），数据来自面板快照本地生成（零请求零新端点）；
RFC4180 转义 + UTF-8 BOM；无面板/空快照如实导出空结构不报错；分享页与
渲染器本体零导出入口（公开只读红线）。

`raw_option` 面板 SHALL 按其自含数据参与整板导出；无法从 `raw_option` 提取
表格化数据时，该面板的 section SHALL 如实标注为不可导出，不中断其余面板。

#### Scenario: 整板导出
- **WHEN** 客户在看板查看态点击导出
- **THEN** 下载单 CSV 含全部面板的 section 数据，与所见快照一致

#### Scenario: 分享页无导出
- **WHEN** 打开公开分享页
- **THEN** 无任何导出入口（渲染器不携带导出 UI）

#### Scenario: 空板如实
- **WHEN** 看板无有效面板或快照全空
- **THEN** 导出仅含标题的空结构文件，不报错

#### Scenario: 不可导出面板不中断整板
- **WHEN** 看板含一个无法表格化的 raw_option 面板与若干普通面板
- **THEN** 普通面板数据照常导出，该面板 section 标注不可导出

### Requirement: 仪表盘 spec v2 契约

dashboard 产物 spec 在 v1 之上扩展为 v2：除标题与面板数组外，面板新增
**类型谱**（在 kpi/line/bar/table 之外引入 candlestick、scatter、pie、
heatmap、gauge、radar、treemap、sankey、funnel、boxplot 十类）、**显式编码
通道**（`encoding` 声明 x/y/系列角色与轴类型，取代"第一列当 x 轴"的位置约定）、
**多窗格**（`panes` 声明共享同一时间轴的副图，对应主图+成交量+技术指标的
分区范式）、**叠加层**（`markLine`/`markArea`/`markPoint` 声明阈值线、周期
分界与事件标记）与 **`raw_option` 逃生舱**（携带完整图表配置直通渲染）。

v2 解析 MUST 向下兼容 v1：无 v2 专有字段的面板按 v1 口径解析并渲染，存量
产物、分享链接与市场摘要行为零变化。宽容校验纪律延续 v1——结构不合法的
面板逐个跳过并标注，不拒绝整个看板；无面板结构的旧自由 JSON 仍按旧版格式
只读呈现。

#### Scenario: v2 面板渲染
- **WHEN** 打开含 candlestick 面板与 time 轴编码通道的 v2 看板
- **THEN** 该面板按声明类型与轴类型渲染，数据来自面板快照

#### Scenario: v1 面板零改动继续渲染
- **WHEN** 打开一个未携带任何 v2 字段的存量 v1 看板
- **THEN** 渲染结果与升级前一致（四类面板按原口径呈现），无需迁移

#### Scenario: 坏面板隔离
- **WHEN** 看板中某面板的 v2 字段形状不合法（编码通道指向不存在的列、类型不在枚举内）
- **THEN** 该面板显示问题标注并被跳过，其余面板正常渲染，整板不失败

### Requirement: 编码通道与轴类型

面板 SHALL 能通过 `encoding` 显式声明数据到图形属性的映射：x 轴列、y 轴
列（可多列，每列一个系列）、可选的系列分组列、可选的大小列。x 轴与 y 轴
SHALL 各自可声明轴类型（类目轴 / 数值轴 / 时间轴 / 对数轴）与轴名。

当面板未声明 `encoding` 时，渲染 SHALL 回退到 v1 的位置约定（第一列为 x、
其余列为系列）并保持类目轴——既有 v1 面板的表达力与渲染结果不变。

时间轴 MUST 支持按时间值连续排布（含不等间隔年份、跨月高频序列），不得
因类目轴排布而在数据缺口处产生等距假象。

#### Scenario: 时间轴正确排布
- **WHEN** 面板编码通道声明 x 为时间轴且快照含不等间隔日期
- **THEN** 图形按真实时间比例排布，缺口处不出现等距压缩或拉伸

#### Scenario: 多系列共享编码
- **WHEN** 面板 y 轴声明多列且未声明系列分组
- **THEN** 每列渲染为一个可独立开关的系列，图例随之出现

#### Scenario: 未声明编码按 v1 回退
- **WHEN** 面板既无 encoding 也无 raw_option
- **THEN** 按第一列 x、其余列系列、类目轴渲染，与 v1 完全一致

### Requirement: 多窗格与副图

面板 SHALL 能声明多个窗格（`panes`）：首个为主窗格，其余为副窗格，垂直堆叠
并**共享同一 x 轴刻度与十字光标**（主图与成交量、技术指标分区联动的既有
行业范式）。副窗格 SHALL 可独立声明自己的编码通道、轴类型与叠加层；窗格
高度 SHALL 可声明，未声明时按主窗格较大、副窗格等分的默认比例分配。

只有一个窗格时，渲染行为 SHALL 与单图面板完全一致。

#### Scenario: 主副图共享时间轴
- **WHEN** 面板声明主窗格（价格）与一个副窗格（成交量）
- **THEN** 两个窗格共享 x 轴刻度，横向缩放与十字光标在两图间同步

#### Scenario: 单窗格零差异
- **WHEN** 面板只声明一个窗格
- **THEN** 渲染结果与不声明 panes 的单图面板一致

### Requirement: 叠加层

面板与窗格 SHALL 能声明叠加层：`markLine`（水平/垂直参考线与阈值线，含
逐线标签与线型）、`markArea`（区间高亮，含区间标签）、`markPoint`（离散
事件标记）。叠加层 SHALL 参与图例与悬浮读数，不得遮挡数据点交互。

未声明叠加层的面板渲染结果 SHALL 与无叠加层能力时完全一致。

#### Scenario: 阈值线呈现
- **WHEN** 面板在 y=2.1 声明一条带标签的 markLine
- **THEN** 图上呈现该阈值线与标签，悬浮时读数包含该线信息

#### Scenario: 区间高亮
- **WHEN** 面板声明一段跨年份的 markArea
- **THEN** 该区间以半透明底色标出并显示区间标签，数据系列仍可正常悬浮

### Requirement: raw_option 逃生舱

面板 SHALL 能携带 `raw_option`（完整图表配置）直通渲染，用于表达类型化
字段覆盖不到的图形能力。当面板同时携带类型化字段与 `raw_option` 时，渲染
SHALL 以类型化字段为准、`raw_option` 仅作未覆盖部分的兜底。

`raw_option` 面板 MUST 在渲染前经过**可序列化校验**：载荷中不得含函数、
正则、类实例等不可序列化值；校验不通过时该面板 MUST 显示问题标注而非
整板失败，且 MUST 如实指明是 `raw_option` 导致。

编辑器对 `raw_option` 面板 SHALL 提供只读的结构查看（不提供手写配置入口），
面板标题、跨度、高度与数据源标注仍可编辑。

#### Scenario: 成品看板整板迁入
- **WHEN** 以 raw_option 面板承载一份既有成品看板的完整图表配置
- **THEN** 渲染结果与该成品独立 HTML 版一致（轴、叠加层、图例、悬浮行为）

#### Scenario: 不可序列化载荷被隔离
- **WHEN** 某面板的 raw_option 含函数值
- **THEN** 该面板显示问题标注并指明原因，其余面板照常渲染

#### Scenario: 类型化字段优先
- **WHEN** 面板同时声明 encoding 与 raw_option
- **THEN** 编码通道按 encoding 生效，raw_option 只补齐未声明的部分

### Requirement: 图表工具栏

控制台查看态与编辑态预览 SHALL 为每个面板提供工具栏（除公开分享页，见
「看板导出」红线）：**全屏查看**（面板独占视口，退出恢复原布局）、
**导出图片**、**在图形与数据表之间切换**、**复制该面板数据**。

全屏与图数切换 SHALL 只改变呈现，不改变 spec、不产生新版本、不发请求。
导出图片 SHALL 在本地生成（零新端点）。分享页 MUST NOT 渲染工具栏。

#### Scenario: 全屏查看
- **WHEN** 客户点击某面板的全屏按钮
- **THEN** 该面板独占视口并重绘至新尺寸，退出后恢复原布局且 spec 未变更

#### Scenario: 图数切换
- **WHEN** 客户切到数据表视图
- **THEN** 同一面板的快照以表格呈现，列名与行值与图形一致，切回图形无损

#### Scenario: 分享页无工具栏
- **WHEN** 打开公开分享页
- **THEN** 任何面板均不出现工具栏、全屏或导出入口

### Requirement: 列表面板契约

看板面板类型谱 SHALL 新增 `list` 类型，用于把文本类快照渲染为图文信息流。
面板 MUST 携带内嵌 `snapshot`（与既有面板同形）；`encoding` MUST 声明
`title` 角色列，MAY 声明 `summary`、`image`、`time`、`source`、`link` 四个
可选角色列，各值均为快照列名。`config.limit` MAY 声明渲染行数上限
（1..20，缺省 20）。

校验 MUST 沿用既有宽容纪律：`title` 缺失、非字符串、或不在快照列内时，
该面板判坏并标注，其余面板照常渲染；可选角色的列名不在快照列内时 MUST
只丢该角色，面板其余部分照常渲染。无 `list` 面板的存量 spec 解析与渲染
MUST 零变化。

#### Scenario: 合法列表面板渲染
- **WHEN** 看板含一个声明 title/summary/image/time/source 五角色的 list 面板
- **THEN** 每个快照行渲染为一张卡片，五角色分别落在图片、标题、摘要、时间与来源位

#### Scenario: 标题列失效隔离
- **WHEN** list 面板的 title 列名不在快照列内
- **THEN** 该面板显示问题标注，同板其他面板正常渲染，看板不整板失败

#### Scenario: 可选角色降级
- **WHEN** list 面板声明了 image 角色但该列不在快照列内
- **THEN** 该面板按无图卡片渲染，不报错、不出现破图占位，其他角色正常

#### Scenario: 存量零漂移
- **WHEN** 打开不含 list 面板的存量看板
- **THEN** 渲染结果与本变更前逐字段一致

### Requirement: 卡片流渲染

list 面板 SHALL 按快照行序渲染卡片流，MUST NOT 在渲染层重排或去重
（顺序与新鲜度由刷新契约的 `query.sort` 决定，渲染与数据表视图同序）。
每张卡片 SHALL 呈现标题、摘要（如声明）、时间（如声明，按本地化短格式
展示原始值）、来源（如声明）与图片（如声明且值非空）。标题 SHALL 最多
两行、摘要最多三行，超出截断；声明 `link` 时卡片 SHALL 可点击跳转。

面板 SHALL 最多渲染 `config.limit` 行（缺省 20）；行数超出面板高度时
SHALL 面板内纵向滚动，不改变页面布局。空快照 SHALL 显示无数据占位。

#### Scenario: 行序即快照序
- **WHEN** 快照行按时间倒序排列
- **THEN** 卡片流按同一顺序呈现，渲染器不重排

#### Scenario: 长文截断
- **WHEN** 某行摘要长度超过三行
- **THEN** 卡片内摘要截断显示，卡片高度不失控，整板布局不变

#### Scenario: 行数上限
- **WHEN** 快照有 50 行且面板未声明 limit
- **THEN** 只渲染前 20 行，面板内可滚动查看

#### Scenario: 空快照诚实呈现
- **WHEN** list 面板快照为空
- **THEN** 显示无数据占位，不空白、不报错

### Requirement: 图文安全底线

list 面板的图片源与外链 MUST 只接受 `http(s)` 协议；`javascript:`、`data:`
与其他协议 MUST NOT 作为可加载源或可跳转目标。图片 MUST 声明懒加载与
`referrerPolicy="no-referrer"`；加载失败 MUST 隐藏图片块，不留破图占位。
外链 MUST 带 `target="_blank"` 与 `rel="noopener noreferrer"`。

标题、摘要、来源 MUST 按纯文本渲染，MUST NOT 解析 Markdown 或产生
`innerHTML` 注入点。此底线对控制台、分享页与橱窗页**同样适用**。

#### Scenario: 脚本协议不外跳
- **WHEN** 某行 link 值为 `javascript:alert(1)`
- **THEN** 卡片不产生可跳转目标，按无链接卡片渲染

#### Scenario: 图片协议过滤
- **WHEN** 某行 image 值为 `data:image/png;base64,...`
- **THEN** 该行不渲染图片块，其余内容照常显示

#### Scenario: 文本不被当作标记
- **WHEN** 某行标题含 `<script>` 或 HTML 标签
- **THEN** 按原文字面显示，不产生 DOM 元素、不执行脚本

### Requirement: 与既有面的兼容口径

list 面板 SHALL 与既有面板同权：整板 CSV 导出按该面板快照行原样导出一个
section（与 table 同口径）；市场与橱窗的面板摘要仍只取 `{id, type, title}`
三字段；分享页与橱窗页 MUST 复用同一渲染器渲染 list 面板，MUST NOT 新增
导出或其他写入入口。刷新侧 MUST 沿用既有口径：`query` 是刷新契约，list
面板的渲染数据源仍是快照。

#### Scenario: 整板导出含列表
- **WHEN** 客户对含 list 面板的看板导出 CSV
- **THEN** 该面板的 section 含快照列头与全部渲染行，与其他面板 section 并列

#### Scenario: 分享页零写入
- **WHEN** 打开含 list 面板的分享页
- **THEN** 卡片可看可点外链，但页面不存在编辑、保存、导出入口

#### Scenario: 刷新不改渲染源
- **WHEN** 带 query 的 list 面板完成一次契约刷新
- **THEN** 新版本按新快照渲染，未刷新时仍按旧快照渲染，渲染不实时查库

### Requirement: dashboard spec SHALL 可经 MCP 产出且与渲染同路径

dashboard v1 spec SHALL 可由 AI agent 经产物构建 MCP 产出；其结构校验 SHALL 走与 spec 更新相同的 `validate_kind_spec` 分派，渲染 SHALL 走与既有看板相同的渲染路径（宽容校验、坏面板隔离、快照呈现）。SHALL NOT 为 MCP 产出物新增第二套 spec 结构或渲染分支。

#### Scenario: MCP 产出的看板正常渲染

- **WHEN** agent 经 MCP 创建一个含 kpi/line/bar/table 面板的 dashboard
- **THEN** 控制台打开该产物时按既有渲染路径呈现，坏面板隔离规则同样生效

#### Scenario: 结构非法同规拒绝

- **WHEN** agent 提交的 dashboard spec 缺 panels 或字段类型错
- **THEN** 422 拒绝且零半写，与控制台路径一致

#### Scenario: 编辑与版本链不受来源影响

- **WHEN** 客户对一个 agent 产出的看板进行编辑
- **THEN** 保存即新版本、契约保留与类型互转等既有行为逐字节一致
