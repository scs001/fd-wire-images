# Proposal: wire-form-entry-advanced（表单高级录入：动态选项 + 选值回填 + 计算字段 + 提交闸）

## Why

真实需求驱动：农业溯源台账系统开发需求清单（finddata 根目录
`农业溯源台账系统开发需求清单.xlsx`，2026-10-15）把「表单录入」撞出了四个
通用缺口——而四个缺口没有一个 是农业专用件，全是任何「主数据—事务数据」
录入场景（订单选客户、报修选设备、台账选档案）都会撞上的平台件：

1. **跨数据集动态选项**：农事记录选农药须从「投入品档案」数据集拉下拉，
   现 select 只支持静态 `options` 或同表单内字段级联 `optionsFrom`；
2. **选值回填**：选定农药后自动带出档案里的安全间隔期天数（值随记录落库，
   档案后续修改不追溯旧记录）；
3. **计算字段**：安全间隔期截止日期 = 记录日期 + 间隔天数，录入时自动算、
   可手动修正（需求书原文语义）；
4. **提交闸**：未到安全间隔期**禁止**采收录入——跨数据集、跨行的提交时
   拦截。质量规则引擎是 refresh 时检测，不在提交路径上，拦不住。

现状还有一个结构隐患：表单字段校验是「公开端后端 `validate_form_values` +
console 端前端镜像」双实现，console 路径后端不执法。提交闸要求服务端成为
唯一执法点，本 change 顺带把这条缝收拢（corrections 端点可选 `artifact_id`，
携带即服务端全量校验，不携带零漂移）。

## What Changes

- **form spec 契约扩展**（`artifacts/form_spec.py`）：field 新增可选键
  `optionsFromDataset`（数据集选项源）、`fillFrom`（选值回填）、`computed`
  （v1 仅 `add_days` 日期加天数，产出可改默认值）；spec 新增可选键
  `constraints[]`（提交闸）。三源互斥：select 的
  `options`/`optionsFrom`/`optionsFromDataset` 恰有其一。
- **跨数据集属主校验**：`optionsFromDataset.datasetId`、`fillFrom` 引用源、
  `constraints[].probe.datasetId` 均须本企业现存数据集（422 零半写，与
  `datasetId` 同规）；引用列是否仍在数据集中不在保存时裁决（沿既有约定）。
- **动态选项解析**：console 运行时经既有查询通道实时去重取值（上限缺省
  200、硬上限 500，超限截断如实标注）；公开分享页新增令牌门选项解析端点
  `GET /api/v1/share/form/{id}/options`（钉死版本、仅声明列+回填映射、
  令牌级限流），并同步修订「公开提交面零行数据读」红线的边界措辞。
- **提交闸服务端执法**：`constraints` 探测（`expect: none|any` + 值可引用
  本表单字段）在公开 submit 与 console corrections（携带 `artifact_id` 时）
  两条路径统一服务端裁决，命中 422 带 message；探测走有效视图（含修正）、
  每表单 ≤10 条、每 probe ≤10 过滤条件。
- **Web**：构建器字段面板支持三新键配置（选项源=数据集+列下拉、回填配置、
  计算配置）；运行时（console 与公开页共用 FormRuntime）动态选项拉取、
  选值回填、计算默认值、提交闸前端镜像预检（UX 层非执法）；约束配置面
  （spec 级 constraints 编辑器）；i18n 双语。
- **文档**：DocsView 表单章节扩写四个新能力的用法与公开选项面边界。

## Impact

- 代码：`api/fd_wire_api/artifacts/{form_spec,service}.py`、
  `share/{routes,rate_limit}.py`、`datasets/{routes,service}.py`
  （corrections 可选 artifact_id）、`web/src/console/form/{formSpec.ts,
  FormEditor.tsx,FormRuntime.tsx,FormPane.tsx}` 等。
- 兼容：不携带新键的存量 spec 行为零漂移；corrections 不带 artifact_id
  行为逐字一致；`select` 三源互斥只对新增第三源生效（存量 options/
  optionsFrom 原样）。
- 红线修订：公开表单面从「零行数据读」修订为「零目标数据集行数据读；
  引用数据集仅暴露声明选项列去重值与回填映射（有界、令牌门、钉死版本）」
  ——design.md D3 有完整裁决。
- 风险：公开选项面是新数据暴露面（有界且令牌门）；cell 档每次提交 ≤10
  probe 的引擎调用成本；双端同规镜像的维护负担（提交闸以服务端为执法点
  缓解）。

## Non-Goals

- **通用表达式语言**：computed v1 只做 `add_days` 结构化单目（需求书唯一
  计算场景：日期+天数），不做字符串表达式解析（防解析面复杂度）；后续按
  需扩 op 枚举。
- **文件/图片字段**（施肥拍照凭证）：属资产上传面，与表单字段类型解耦，
  另行立项。
- **业务编号生成器**（批次号规则）：数据集层能力非表单层，另行立项。
- **提交闸的写路径拦截以外的动作**（如自动改值、级联写多表）：不做，
  闸只裁决「允许/拒绝 + message」。
