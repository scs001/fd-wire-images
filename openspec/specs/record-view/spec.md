# record-view Specification

## Purpose
定义单记录公开页产物（kind=record-view）：以「主数据集+键列+展示模板」声明
一个按键参数化的公开只读页——扫码/输键即取该键记录与其关联时间轴（引用
数据集按声明过滤），实时反映有效视图（含修正）。这是平台首个公开实时数据
读面，读边界、令牌与限流为一等契约。服务溯源页/报告页/证书页等按键展示
场景。

## Requirements

### Requirement: record-view 产物契约

record-view 产物 spec 信封MUST为 `{title, spec:{datasetId, keyColumn,
headerFields[], sections[]}}`：`datasetId` 为主记录数据集（本企业现存，他企/
不存在 422 零半写）；`keyColumn` 为主记录键列（如批次号，保存时须在该数据
集列快照内）；`headerFields` 为 `[{column, label?}]`（column 须属主记录
数据集列快照、表单内唯一；label 缺省=列名）；`sections` 为 `[{title,
datasetId, dateColumn, filters[], limit?}]`（每个 section 声明一个时间轴
源：datasetId 本企业现存、dateColumn 须在该数据集列快照内、filters 为
`[{column, op∈eq|in|gt|gte|lt|lte, value}]` 且 value 为标量/标量数组或
`{fromField: <主记录列>}`（运行时以主记录行的该列值代入）、limit 1..500
缺省 200）。headerFields ≤50 条、sections ≤10 条、每 section filters
≤10 条；形状/属主非法一律 422 定位到字段，零半写。空白创建生成最小空
spec（datasetId 为空、零 header、零 section），未绑数据集的中间态可保存、
不可生成分享。spec 保存走产物既有版本链（append-only）；keyColumn、
headerFields.column、sections.dateColumn 与 filters 的 `{fromField}` 按
保存时刻列快照校验（同 entity-layer 主键列口径）；section 数据集的过滤列
与日期列后续漂移在取数时以单段 error 如实标注（不静默空段）。

#### Scenario: 空白创建
- **WHEN** 客户以 kind=record-view 空白创建并起标题
- **THEN** 生成 draft 产物（未绑数据集的最小 spec），可进构建器

#### Scenario: 溯源页声明
- **WHEN** 客户配置主数据集=批次集（keyColumn=批次号）、headerFields=
    [产品名称,产地,负责人,联系电话]、sections=[播种/施肥/打药/采收四源，
    filters 引用 {fromField: 地块编号}] 并保存
- **THEN** 保存通过；发布后可生成分享链接与按批次取数

#### Scenario: 引用失效如实
- **WHEN** 取数时某 section 的 dateColumn 已不在其数据集列快照中
- **THEN** 该 section 返回可读错误标注（不静默空段），其余 section 与头部
    正常返回

### Requirement: 公开实时取数端点与读边界

published record-viewMUST提供公开取数端点 `GET /api/v1/share/record-view/
{id}?t=<令牌>&k=<键值>`：令牌验签+钉死版本（令牌机器/统一 404/禁用 503
语义与 dashboard-share 一致）；按 `{column: keyColumn, op: eq, value: k}`
查主数据集有效视图（含修正）至多一行——零行返回结构化「无此记录」态
（200 非错误，页面如实呈现，不泄露是否存在过）；命中后组装：主记录行的
headerFields 值 + 各 section 按 filters（`{fromField}` 以主记录行值代入）
过滤、按 dateColumn 升序、至多 limit 行。读边界MUST收在：仅执行钉死版本
spec 声明的查询（主记录键等值 + section 声明过滤），不开放任意查询/任意
列/任意数据集；同一令牌可查该产物任意键值（k 不进令牌，枚举边界如实
文档）。令牌级滑动窗限流MUST执行（缺省 240 次/小时，env
FD_WIRE_SHARE_RECORDVIEW_RATE_PER_HOUR 可调，超限 429 带 Retry-After）；
服务端按（产物,版本,令牌,k）做 60 秒短缓存。页面MUST带 noindex 与分享页
既有品牌面四态规范。

#### Scenario: 扫码取数
- **WHEN** 持有效令牌者带批次号 k 请求取数端点
- **THEN** 返回该批次头部字段值与四类农事记录时间轴（升序、含修正后的
    有效视图），页面移动端呈现时间轴

#### Scenario: 无此批次
- **WHEN** k 在主数据集中零行命中
- **THEN** 返回「无此记录」态页面（200），错误形态一致不泄露历史存在性

#### Scenario: 补录后扫码可见
- **WHEN** 批次某农事记录在分享链接生成后被补录（insert 修正）
- **THEN** 下次扫码该节点出现在时间轴（缓存 TTL 内至多延迟 60 秒）

#### Scenario: 声明外查询拒
- **WHEN** 任何试图以非声明列/非声明数据集/任意过滤形态取数的请求
- **THEN** 无此端点形态可用（404/422 如实），公开面不因新能力扩大查询面

#### Scenario: 令牌限流
- **WHEN** 同一令牌请求频率超过限流窗
- **THEN** 429 带 Retry-After，超限请求不落查询

### Requirement: 二维码与分享链接生成面

record-view 详情面MUST提供分享与二维码生成：键值选择器（主数据集
keyColumn 去重值下拉，上限 500，或手输键值）→ 展示完整分享 URL
（`#/share/record-view/{id}?t=…&k=…`）→ 下载二维码（PNG 高清与 SVG 两种，
纯前端生成、零后端依赖）。URL 与二维码随生成时刻令牌钉死版本；令牌被
撤销（归档/取消发布）后既有二维码指向失效页（404 态）。

#### Scenario: 生成批次二维码
- **WHEN** 客户在 record-view 详情选某批次号生成
- **THEN** 得到该批次的分享 URL 与可下载二维码图，扫码打开该批次取数页

#### Scenario: 撤销即失效
- **WHEN** 产物被归档后扫描既有二维码
- **THEN** 打开统一 404 失效页

### Requirement: 构建器与试预览

console MUST提供 record-view 构建器：主数据集选择（本企业列表）→ 键列
选择 → headerFields 勾选与 label 改写 → sections 增删配（数据集/日期列/
filters 字面或引用主记录字段/上限/标题）→ 示例键值试预览（console 身份
按同一取数组装逻辑渲染预览，不生成公开链接）。草稿校验 MUST 与后端保存
校验同规（前端镜像）；未绑数据集中间态可保存不可分享（如实提示）。
i18n zh/en。

#### Scenario: 配置全流程
- **WHEN** 客户在构建器完成主数据集/键列/头部/section 配置并保存
- **THEN** 保存通过，可发布并生成分享链接

#### Scenario: 试预览同规
- **WHEN** 客户在构建器选示例批次号点试预览
- **THEN** 预览呈现与公开扫码页同构的时间轴（console 身份取数，同一组装
    逻辑）

### Requirement: 展示面收录与文档红线

workbench 徽标与产物页展示序MUST收录 record-view（api 与 web 两处展示序
保持一致）；web 文档页MUST随本能力同批新增「单记录公开页」章节（zh/en）：
构建/发布/分享/二维码用法，**公开实时读面的暴露边界如实说明**（仅钉死
版本声明的查询、令牌级而非记录级隔离、k 可枚举、限流与缓存口径）；AI 产
物门（ARTIFACT_TOOL_KINDS）不收录 record-view。

#### Scenario: 展示序一致
- **WHEN** 本能力上产后打开工作台与产物页
- **THEN** record-view 出现在徽标与列表（api/web 两处一致），AI 生成门
    不出现该 kind

#### Scenario: 文档边界如实
- **WHEN** 阅读文档「单记录公开页」章节
- **THEN** 可知公开页暴露哪些数据、令牌门粒度与撤销语义，无「按记录隔离」
    的误导表述
