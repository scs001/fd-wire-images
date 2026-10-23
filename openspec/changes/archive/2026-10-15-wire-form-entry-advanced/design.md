# Design: wire-form-entry-advanced

## Context

代码现状（2026-10-15 探锚）：

- **spec 校验单点**：`api/fd_wire_api/artifacts/form_spec.py:208`
  `validate_form_spec`（FIELD_INPUTS :50、options/optionsFrom 互斥 :247-260、
  联动引用二趟裁决 :278-317）；属主校验在 `artifacts/service.py:97`
  `_require_form_dataset`。
- **提交双路径**：写路径已统一到 `datasets/service.py:385`
  `submit_insert_correction`；但字段校验双实现——公开端后端
  `form_spec.validate_form_values`（`share/routes.py:345` 调用），console 端
  `datasets/routes.py:719-758` corrections 端点**不做表单字段校验**，靠前端
  镜像 `web/src/console/form/formSpec.ts:627` 先拦。
- **选项解析地基已在**：api 档 `datasets/api_store.py:292`
  `execute_query(filters…, distinct_column…)`（SQL 面 distinct :365-386、
  修正合并面 :1006）；引擎面 TOOL_QUERY 同样支持 distinct；数据面端点
  `data_plane/data_routes.py:188` 接受 console（Logto ID token）身份。
- **公开面零数据读**：`share/routes.py` 只有 form spec 快照 GET（钉死版本）
  与 submit POST；限流器 `share/rate_limit.py`（mac 段为键的滑动窗）可复用。
- **web 运行时**：console 与公开页共用 `web/src/console/form/FormRuntime.tsx`
  （字段渲染 :206-272、select 动态选项解析 :86-119、隐藏清值 :66-80）；
  求值函数集中在 `formSpec.ts`（`resolveSelectOptions` :551、
  `evaluateFieldVisibility` :574、`validateFormValues` :627）。

## Goals / Non-Goals

**Goals:**

- 四个通用录入原语（动态选项/回填/计算/提交闸）落进 form spec 契约与
  双端运行时，存量 spec 零漂移。
- 提交闸服务端唯一执法，console 与公开两路径同规。
- 公开选项解析端点以最小暴露面（声明列去重值+回填映射、有界、令牌门）
  服务免登录录入员。

**Non-Goals:**

- 通用表达式语言、文件字段、编号生成器、闸的自动改值/级联多表写
  （proposal Non-Goals 已列）。
- 引用数据集的行级权限模型（引用即读整列去重值，属表单属主的数据授权
  范畴，不新造权限面）。

## Decisions

**D1 服务端执法点：corrections 可选 `artifact_id`。**
提交闸必须双路同规，落 `submit_insert_correction` 之外的表单感知层：
corrections 请求体新增可选 `artifact_id`；携带时验「本企业 + kind=form +
spec.datasetId == 目标集」→ 按当前版本执行 `validate_form_values`（扩展后）
+ constraints；不携带逐字零漂移。公开 submit 端点在同一共享函数
`validate_form_submission(spec, values, resolver_ctx)` 内跑字段校验+闸，
两路径共用该函数（console 传当前版本，公开传钉死版本）。**不选**「把闸
塞进 submit_insert_correction」：该函数无产物上下文，且 cell 档透传路径
（`service.py:430-440`）不该感知表单。

**D2 动态选项三源互斥 + 双端同规解析。**
select 选项源恰其一（options/optionsFrom/optionsFromDataset）。解析口径
统一为「有效视图去重非空值、升序、≤limit、截断标注」：console 走
`POST /api/v1/datasets/{id}/query`（filters+distinct_column，console 身份
已可过 `data_plane/auth.py:47-75`，免新后端面）；公开走新端点
`GET /api/v1/share/form/{id}/options`；提交校验由服务端实时解析裁决（复用
`_query_dataset` tier 分流：免费档本地、cell 档引擎 TOOL_QUERY）。引用失效
（集删/列不在快照）= 空集+可读错误标注，不静默。

**D3 公开选项端点的暴露边界（红线修订）。**
原「公开提交面零行数据读」修订为「零目标数据集行读 + 受限引用读」。端点
返回体按钉死版本静态可推导：`{fields: {column: {options[], truncated,
fills: {fill目标列: {选项值: 值}}}}}`——只含声明列的去重值与回填映射，
不开放任意查询。令牌验签/统一 404/限流复用 `share/service.py` 既有机器
（`_mac`/`verify_token`/`rate_limit.py`，阈值独立 env）。服务端对响应做
60 秒短缓存（同 token 同版本同产物），公开页反复刷新不放大查询。

**D4 computed 只做结构化 add_days。**
`{op:"add_days", date:<date字段>, days:<number字段>|<int>}`，求值发生在
web 运行时（console 与公开页共用 FormRuntime 的同一实现，闰年/月末进位
一致）；服务端不重算（可手改语义——提交只做 date 类型校验，不强制等于
重算，需求书「系统自动计算，可手动修正」原义）。求值失败（缺值/非法）
留空。后续扩展走 op 枚举追加（如 sub_days/mul），不引表达式串。

**D5 probe 走存在性查询，值代入以提交 values 为准。**
`expect none|any` 语义映射：none=探测到行即拦（禁采场景）、any=零行即拦
（前置存在场景，如「批次号必须已存在」）。filters 的 `{field: col}` 值在
求值时从提交 values 取（按字段 input 类型转义），探测复用 `_query_dataset`
（`query_contract.py:183`）limit=1。字段校验先于闸（用户先看到字段错再
看到业务拦）。cell 档成本上界：每次提交 ≤10 probe × 1 次引擎调用。

**D6 回填=可改默认值 + 落库快照语义。**
回填值随记录落库，不与源行强制一致（档案改版不追溯旧记录——台账审计
要求快照）。console 回填随选项选择即查（limit=1 等值查询）；公开页由
选项端点 fills 映射一次带出（免逐选项请求）。

## Risks / Trade-offs

- **公开选项面是新暴露面**：暴露的是表单属主声明列的去重值——等于把
  「引用数据集该列的全部不同值」给了持链接者。边界已在 D3 收紧（声明列/
  有界/令牌门/限流/短缓存），文档如实说明（文档红线 requirement）。
- **提交时实时解析的时序语义**：选项在渲染后被档案修正改变 → 提交 422。
  这是刻意的防篡改语义（提交值必须存在于提交时刻档案），错误文案指导
  用户重选，不静默接受旧值。
- **双端镜像维护负担**：formSpec.ts 镜像面扩大（三键求值+闸预检）。以
  服务端为唯一执法点兜底，前端仅 UX 预检；校验用例表双端共享同一份
  语义描述（任务 6.3 的双向一致性用例）。
- **cell 档提交延迟**：动态选项解析+至多 10 probe 引擎调用叠加在提交路径
  上；上界明确（≤11 次），超时口径沿用引擎客户端既有配置。
