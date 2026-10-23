# Tasks: wire-form-builder

## 1. api — form 产物契约与出生

- [x] 1.1 `api/fd_wire_api/artifacts/form_spec.py`：form spec 结构校验纯函数
  （信封/datasetId 可空/fields 结构/input 枚举/column 唯一/select options/
  文案长度），错误文案逐条可断言
- [x] 1.2 `artifacts/service.py`：`BLANK_CREATE_KINDS=("dashboard","form")`、
  form 初始 spec（datasetId=None+fields=[]）；`update_spec`/`create_blank_`
  `artifact` 按 kind 分派校验（dashboard 语义零漂移）
- [x] 1.3 `update_spec` 保存时 datasetId 非空 → datasets 库会话验存在+属主
  （他企/不存在 422 零半写；注意生产双库——照 query_contract 先例路由会话）
- [x] 1.4 pytest：空白创建/结构校验矩阵/属主校验/dashboard 回归（既有
  artifacts 测试全绿零漂移）

## 2. api — 提交路径下沉与运行时语义

- [x] 2.1 `datasets/service.py::submit_insert_correction`：insert 分支自
  corrections 路由下沉（api 档本地层含行数闸/cell 档引擎透传，actor 入参化）；
  corrections 路由改调它（行为零漂移，既有测试全绿为证）
- [x] 2.2 pytest：下沉后 corrections 既有用例零改动通过（同闸同审计）

## 3. api — 分享扩展与公开提交

- [x] 3.1 `share/service.py`+`routes.py`：生成端点 kind∈{dashboard,form}+路径
  分派；公开 `GET /api/v1/share/form/{id}`（钉死版本、统一 404、secret 禁用
  503）
- [x] 3.2 `share/rate_limit.py`：令牌级滑动窗限流器（mac 段为键、惰性 env、
  Retry-After），单测覆盖窗滑动与并发安全（线程内锁语义）
- [x] 3.3 公开 `POST /api/v1/share/form/{id}/submit`：校验序按 design D3
  （统一 404 / 422 字段级 / 限流 429 / 行数闸 429 / 引擎 502 / 201 成功）；
  values 类型校验纯函数（number/date/select/text 规则）可独立测
- [x] 3.4 pytest：公开提交全矩阵（成功落审计 actor=share-form:{id}、钉死版本
  校验、未知列/必填/类型、限流、归档断链 404、kind 门、他企数据集 404）

## 4. web — 构建器/运行时/分享页

- [x] 4.1 `console/form/formSpec.ts`：spec 纯函数（列勾选→字段、结构自检、
  与后端规则同源）；`ARTIFACT_KINDS`+i18n kinds.form+徽标
- [x] 4.2 `console/form/FormRuntime.tsx`：共用运行时（endpoint props、必填/
  类型前端拦、逐字段错误、successMessage、未绑数据集诚实态）+ 单测
- [x] 4.3 `console/form/FormEditor.tsx`：数据集选择→列勾选→字段卡配置→
  文案→保存版本链 + 单测
- [x] 4.4 ArtifactsPage：新建表单入口、form 详情展开（运行时+编辑器+分享
  按钮）+ 单测（含免费档路径——零 cell 零 AI 出生）
- [x] 4.5 ShareView：`/share/form/:id` 态（FormRuntime 公开端点+品牌/noindex
  沿用）+ 单测；vitest+tsc 全绿

## 5. docs — 双语教程（红线⑥）

- [x] 5.1 web 文档页「表单」章节 zh/en（创建→绑数据集→配字段→试填→发布→
  分享→收集回数据集/修正历史回滚），i18nKeys 完整性测试过

## 6. 验证与发布（主线程）

- [x] 6.1 门禁：api pytest 全绿 / web vitest+tsc 零错 / `openspec validate
  wire-form-builder --strict` 零 error
- [x] 6.2 发布：commit → push scs001（CI 绿）→ relay 首见携带 → manifest
  滚版 → ArgoCD 同步 → 公网冒烟（空白建表单→绑定→提交→分享→公开提交→
  修正历史回滚全链）
- [x] 6.3 归档 `wire-form-builder`（validate --strict 过后 archive），主 spec
  合并核对（form-apps 新建/artifact-lifecycle+dashboard-share 修订）
