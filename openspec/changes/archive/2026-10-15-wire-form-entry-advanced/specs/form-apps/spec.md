# form-apps Delta（wire-form-entry-advanced）

## MODIFIED Requirements

### Requirement: 表单产物契约

form 产物 spec 信封MUST为 `{title, spec:{datasetId, fields[], submitLabel?,
successMessage?, constraints?}}`；fieldMUST为 `{column, label, input:
text|number|date|select, required, options?, optionsFromDataset?, fillFrom?,
computed?}`（**select 的选项三源 `options`/`optionsFrom`/
`optionsFromDataset` 恰有其一**；column 在表单内唯一）。空白创建MUST生成最小
空表单 spec（零字段、datasetId 为空）；spec 保存MUST走产物既有版本链
（append-only）；datasetId 非空时MUST指向本企业现存数据集（他企/不存在
422，零半写）；列名是否仍在数据集中不在保存时裁决（构建器呈现实时列，
提交时如实校验）。

新增可选键形状与保存校验：`optionsFromDataset` 为 `{datasetId, column,
filters?, limit?}`（filters 为 `[{column, op∈eq|in|gt|gte|lt|lte, value}]`
字面量条件，value 为标量或标量数组（仅 in）；limit 为 1..500 整数、缺省
200）；datasetId 必须指向本企业现存数据集（422 零半写，同 datasetId 口径）。
`fillFrom` 为 `{field, column}`（field 必须引用同表单内带
optionsFromDataset 的 select 字段，不得自引用）；column 属主校验同上。
`computed` 为 `{op: "add_days", date, days}`（date 必须引用同表单内
input=date 的**另一**字段；days 引用同表单内 input=number 字段或非负整数
字面量；不得自引用）。`constraints` 为 `[{id, message, when?, probe}]`
（id 在表单内唯一非空；message 非空 ≤500；when 为单条件显隐同形条件；probe
见「提交闸与统一执法点」）。新键的引用存在性/互斥/形状非法一律 422 定位到
字段，零半写；不带新键的存量 spec 校验行为逐字不变。

#### Scenario: 空白创建
- **WHEN** 客户以 kind=form 空白创建表单并起标题
- **THEN** 生成 draft 产物（v1=零字段未绑数据集的最小 spec），可进构建器

#### Scenario: spec 结构校验
- **WHEN** 保存的 form spec 缺 title、字段 input 越枚举、column 重复、
    select 无选项源或携带两个及以上选项源、computed 引用非日期字段、
    fillFrom 引用非动态选项 select
- **THEN** 422 如实拒绝且零半写（不追加版本行），错误定位到具体字段

#### Scenario: 数据集属主校验
- **WHEN** spec.datasetId、optionsFromDataset.datasetId、fillFrom 引用源或
    probe.datasetId 指向不存在或他企数据集
- **THEN** 422 如实拒绝，不落版本

#### Scenario: 存量零漂移
- **WHEN** 保存不带任何新键的既有表单 spec
- **THEN** 校验与保存行为与本变更前逐字一致

### Requirement: 表单分享与公开提交

published 表单MUST可生成签名分享链接（令牌机器/TTL/禁用语义与 dashboard-share
一致）；公开 GETMUST返回钉死版本 spec（仅字段定义与文案，**零目标数据集行
数据**）；公开 `POST /submit` 接收 `{values}` 并MUST按钉死版本 spec 校验——
未知列 422、必填缺失 422、number/date/select 类型不符 422（number 收数值
或数字串、date 须 YYYY-MM-DD、**select 须在按选项三源服务端实时解析的
选项集内**——optionsFromDataset 以提交时刻有效视图去重值为准）——再经
提交闸裁决（见「提交闸与统一执法点」）；通过后MUST按目标数据集 tier 走与
运行时相同的 insert 修正路径，actor=`share-form:{artifact_id}`（审计可
归因到具体表单）；令牌级滑动窗限流MUST执行（缺省 60 次/小时，env
FD_WIRE_SHARE_SUBMIT_RATE_PER_HOUR 可调，超限 429 带 Retry-After）；
令牌/发布态/版本行任一不满足MUST统一 404。

公开面读边界修订：原「公开提交面零行数据读路径（写-only）」修订为——
公开面**不读目标数据集行数据**；唯一新增读面为选项解析端点
`GET /api/v1/share/form/{id}/options`（令牌验签 + 钉死版本）：仅返回该版本
声明了 `optionsFromDataset` 的字段之选项去重值（按值升序、上限截断如实
标注）与 `fillFrom` 回填映射（每选项值一行声明列取值），不返回任何其他
行/列；令牌级滑动窗限流MUST执行（缺省 120 次/小时，env
FD_WIRE_SHARE_OPTIONS_RATE_PER_HOUR 可调）；令牌/发布态/版本行任一不
满足MUST统一 404。

#### Scenario: 公开提交成功
- **WHEN** 持有效令牌者按字段填写并提交
- **THEN** 201 返回成功文案，数据集落 insert 修正（actor=share-form:{id}），
    所有者可在修正历史看到该条并可回滚

#### Scenario: 钉死版本校验
- **WHEN** 表单改版出新版本后，旧令牌提交
- **THEN** 字段集与提交闸按令牌钉死版本执行（旧字段集），动态选项解析仍以
    提交时刻数据为准

#### Scenario: 字段校验拒绝
- **WHEN** 提交含未定义列、缺必填、类型不符，或 select 值不在服务端实时
    解析的选项集内（含动态选项源）
- **THEN** 422 如实指明原因，数据集零写入

#### Scenario: 令牌限流
- **WHEN** 同一令牌提交频率超过限流窗
- **THEN** 429 带 Retry-After，超限提交不落库

#### Scenario: 选项解析端点
- **WHEN** 持有效令牌者 GET 选项解析端点
- **THEN** 返回钉死版本声明的动态选项字段之去重选项（升序、截断标注）与
    回填映射；未声明动态选项的表单返回空集而非 404

#### Scenario: 统一 404
- **WHEN** 令牌无效/过期/产物非 published/kind 非 form
- **THEN** 一律 404 且错误形态一致，不泄露差异原因

### Requirement: 联动改选项

select 字段 SHALL 支持选项联动：可选键 ``optionsFrom`` 为
``{field, cases: {控制值: [选项...]}, fallback?: [选项...]}`（与静态
options 及 optionsFromDataset 互斥，select 必须恰有其一；field 引用同
表单另一字段，自引用/不存在拒）。求值（双端逐字同规）：按 raw values
解析控制值（trim 字符串口径）命中 cases 取其列表，未命中/空值取
fallback，无 fallback 为空列表；**解析后选项为空的字段按隐藏同规**
（不可填即不可见）；提交时 select 值必须 ∈ 解析后选项（公开提交端点以
raw values 重解析，防篡改同规 422）；web 运行时在选项变化且现值不在新
选项集时清值（不进载荷）。

#### Scenario: 级联选项
- **WHEN** select 带 optionsFrom（省→市映射）且控制字段值为命中的省
- **THEN** 运行时呈现该省选项列表；提交属于该列表的值通过、伪造不属于的值被 422 拒

#### Scenario: 空选项按隐藏
- **WHEN** 控制字段未填（无 fallback）→ 解析选项为空
- **THEN** 该字段视同隐藏：携带非空值被拒、required 不检查、空值跳过不写键

#### Scenario: 与静态选项互斥
- **WHEN** select 同时携带 options 与 optionsFrom
- **THEN** 保存被拒，文案定位字段与互斥原因

#### Scenario: 与数据集选项源互斥
- **WHEN** select 同时携带 optionsFrom 与 optionsFromDataset（或 options
    与 optionsFromDataset）
- **THEN** 保存被拒，文案定位字段与互斥原因

## ADDED Requirements

### Requirement: 跨数据集动态选项

select 字段MUST支持选项源 `optionsFromDataset: {datasetId, column,
filters?, limit?}`：选项=该数据集有效视图（含修正）按 filters 过滤后
column 列的去重非空值，按值升序，至多 limit 条（缺省 200、硬上限 500），
超限截断并如实标注 truncated。解析MUST双端一致：console 运行时以会话
身份经数据面查询端点（filters+distinct_column）解析；公开页经选项解析
端点解析；提交校验（两路径）MUST以服务端实时解析的选项集裁决（值不在
集内 422，防篡改）。引用数据集被删除或列已不在快照中时，解析结果为空集
+ 如实错误标注（选项列缺失不静默），提交该字段非空值被 422 拒。

#### Scenario: 档案下拉
- **WHEN** 农事表单的「农药名称」字段带 optionsFromDataset 指向投入品
    档案数据集的名称列，运行时打开
- **THEN** 下拉呈现该列去重值（升序、≤limit）；选择后正常提交

#### Scenario: 提交时实时裁决
- **WHEN** 提交的 select 值不在提交时刻服务端解析的选项集内（如伪造值或
    档案行已被修正删除）
- **THEN** 422 如实拒，数据集零写入

#### Scenario: 引用失效如实
- **WHEN** 引用数据集已删除或 column 已不在其列快照中
- **THEN** 运行时选项区呈现可读错误（不静默空白）；提交该字段非空值 422

#### Scenario: 截断标注
- **WHEN** 去重值超过 limit
- **THEN** 返回前 limit 条并带 truncated 标注，不谎报全量

### Requirement: 选值回填

字段MUST支持可选 `fillFrom: {field, column}`：其 fieldMUST为同表单内带
optionsFromDataset 的 select。运行时当该 select 取值 v 时，从其引用数据集
有效视图按「选项列=v」取至多一行，将该行 column 值填入本字段作为**可改
写的默认值**（用户可覆盖；填不到留空不报错）。回填值作为普通字段值提交
（按其 input 类型校验），MUST不与源行强制一致——记录落库即快照语义
（档案后续修改不追溯旧记录）。公开页的回填映射由选项解析端点随选项一并
返回（每选项值×声明回填列，有界）。

#### Scenario: 选农药带出间隔期
- **WHEN** 「安全间隔期天数」字段 fillFrom 指向农药 select 的引用档案的
    间隔期列，用户选定某农药
- **THEN** 天数字段自动填入该行间隔期值，用户可手改，提交按 number 校验

#### Scenario: 选值后再改不回滚手改
- **WHEN** 用户改选另一农药后再次自动回填
- **THEN** 以新回填值覆盖（回填即最新默认），此后的手改继续有效

#### Scenario: 源行缺失留空
- **WHEN** 选中值在引用数据集中已无对应行
- **THEN** 回填字段留空，不报错不阻断提交（除非该字段本身 required）

### Requirement: 计算字段

字段MUST支持可选 `computed: {op: "add_days", date, days}`（v1 仅此一 op）：
运行时在 date 或 days 任一参与值有效时求值——date 须为合法 YYYY-MM-DD、
days 须为非负整数（引用字段或字面量）——产出 date+days 天的 YYYY-MM-DD
作为**可改写的默认值**（自动重算覆盖，用户可手改覆盖重算）；参与值缺失/
非法时留空不报错。提交按 date 类型校验，MUST不强制等于重算值（可手动
修正语义）。求值在 web 运行时同规（console 与公开页共用同一求值实现，
含闰年/月末进位）；提交侧服务端仅按类型校验、不重算。

#### Scenario: 安全间隔期截止日
- **WHEN** 「截止日期」字段 computed=add_days(记录日期, 安全间隔期天数)，
    两参与字段均已填
- **THEN** 截止日期自动填入（如 2026-10-15 + 7 = 2026-10-22），可手改，
    提交按日期校验通过

#### Scenario: 参与值缺失留空
- **WHEN** days 引用字段为空或非整数
- **THEN** 计算字段留空，不报错；required 时按必填校验拦截提交

### Requirement: 提交闸与统一执法点

spec 可选 `constraints: [{id, message, when?, probe: {datasetId, filters,
expect}}]`：probe 为对引用数据集有效视图（含修正）的存在性探测——filters
为 `[{column, op∈eq|in|gt|gte|lt|lte, value}]`，value 为标量/标量数组或
`{field: <本表单字段列>}`（以提交 values 按其 input 类型解析代入）；
`expect: "none"` 表示**探测到任一行即拦截**、`expect: "any"` 表示**零行
即拦截**；`when` 为单条件显隐同形条件（以提交 values 求值，真才执行探测）。
约束MUST在服务端执法且两提交路径统一：公开 submit 端点与 console
corrections 端点（请求携带可选 `artifact_id` 且其为本企业 kind=form、
spec.datasetId 与目标数据集一致时——此时服务端按当前版本执行全量字段
校验+提交闸；不携带或校验不符 422 定位原因，**不携带时行为与现状逐字
一致**）。命中拦截 MUST 422 带 constraint id 与 message，数据集零写入；
字段校验失败先于提交闸呈现。每表单 constraints ≤10 条、每 probe filters
≤10 条（保存校验）；探测按 limit=1 存在性执行。

#### Scenario: 禁止违规采收
- **WHEN** 采收表单带 constraint（probe 打药记录集，filters
    [{column: 地块, value: {field: 地块}}, {column: 截止日期, op: gte,
    value: {field: 记录日期}}]，expect none），提交的采收日期仍处安全
    间隔期内
- **THEN** 422 带该 constraint 的 message，采收记录零写入

#### Scenario: console 路径同规执法
- **WHEN** console 运行时提交（corrections 携 artifact_id）命中同一约束
- **THEN** 服务端同样 422 拦截，与公开路径行为一致

#### Scenario: 不携带 artifact_id 零漂移
- **WHEN** 既有调用方向 corrections 端点提交且不带 artifact_id
- **THEN** 行为与现状逐字一致（不执行表单字段校验与提交闸）

#### Scenario: 条件闸按需生效
- **WHEN** constraint 带 when 条件且提交 values 求值为假
- **THEN** 不执行探测不拦截，提交按其余校验继续

### Requirement: 构建器高级录入配置

console 表单构建器MUST支持三新键与提交闸的配置面：字段面板按字段类型
提供——选项源配置（数据集下拉=本企业列表、列下拉=该集列快照、可选过滤
条件与上限）、回填配置（源字段下拉限本表单动态选项 select、列下拉限该
引用集列）、计算配置（op 固定 add_days、日期字段下拉限 date 字段、天数
可选字段下拉或字面量输入）；spec 级约束编辑器（增删改 constraint：探测
数据集/过滤条件（值=字面或引用字段）/expect/message/when）。构建器草稿
校验与后端保存校验同规（前端镜像扩展）。运行时（console 与公开页共用
FormRuntime）MUST实现动态选项拉取、选值回填、计算默认值、提交闸前端镜像
预检（UX 层预检，执法以服务端为准）。i18n zh/en 同步。

#### Scenario: 配置动态选项全流程
- **WHEN** 客户在构建器为某 select 选择选项源（数据集+列）并保存
- **THEN** 保存通过，运行时该字段下拉呈现解析选项

#### Scenario: 配置提交闸
- **WHEN** 客户在约束编辑器添加探测约束（引用字段作过滤值）并保存
- **THEN** 保存通过；试提交命中时前端预检与服务端拦截呈现同一 message

#### Scenario: 非法配置前端即拦
- **WHEN** 构建器中把 fillFrom 指向非动态选项 select
- **THEN** 前端草稿校验定位字段拒绝，保存不可达

### Requirement: 文档与红线

web 文档页「表单」章节MUST随本能力同批扩写：动态选项/选值回填/计算字段/
提交闸的配置与语义、公开选项解析端点的数据暴露边界（仅声明选项列去重值
与回填映射、有界、令牌门）、console 提交携带 artifact_id 的服务端执法
语义；i18n 双语。公开面（未登录）除选项解析端点外不新增任何数据读路径。

#### Scenario: 教程同批
- **WHEN** 本能力上产
- **THEN** 文档页表单章节含四个新能力小节（zh/en）与公开选项面边界说明

#### Scenario: 公开面边界
- **WHEN** 未持令牌调用选项解析端点
- **THEN** 统一 404，与既有公开面错误形态一致
