# Tasks: wire-form-entry-advanced

## 1. 后端 spec 契约扩展

- [x] 1.1 `artifacts/form_spec.py`：field 三新键（optionsFromDataset/fillFrom/
  computed）形状与互斥校验（select 三源恰其一、fillFrom 源必须为动态选项
  select、computed 引用类型约束与自引用拒），错误定位到字段
- [x] 1.2 `artifacts/form_spec.py`：spec 级 `constraints` 校验（id 唯一、
  message 长度、when 单条件同形、probe filters 算子白名单、value 形状
  标量|{field}、expect 枚举、≤10 条/≤10 filters）
- [x] 1.3 `artifacts/service.py`：新键引用数据集属主校验（复用
  `_require_form_dataset` 口径：optionsFromDataset.datasetId、fillFrom 源、
  probe.datasetId，他企/不存在 422 零半写）
- [x] 1.4 存量 spec 零漂移回归：不带新键的保存/加载/提交用例逐字一致

## 2. 后端动态选项解析

- [x] 2.1 服务端选项解析函数（复用 `_query_dataset` tier 分流：免费档
  `execute_query` distinct、cell 档引擎 TOOL_QUERY）：去重非空值、升序、
  ≤limit、截断标注；引用失效返回空集+错误标注
- [x] 2.2 console 查询通道验证：`POST /api/v1/datasets/{id}/query`
  （filters+distinct_column）以 console 身份可用（data_plane 既有轨道），
  无需新端点
- [x] 2.3 公开选项解析端点 `GET /api/v1/share/form/{id}/options`：令牌
  验签+钉死版本、返回声明字段选项与 fills 回填映射、统一 404、令牌级
  限流（缺省 120/h，env FD_WIRE_SHARE_OPTIONS_RATE_PER_HOUR）、60s 响应
  短缓存

## 3. 后端提交闸与统一执法

- [x] 3.1 共享提交校验函数 `validate_form_submission`：字段校验（扩展
  `validate_form_values` 支持三源选项实时裁决+computed 类型校验）→
  constraints 探测（when 求值、{field} 值代入、limit=1 存在性、expect
  none/any、命中 422 带 id+message）
- [x] 3.2 公开 submit 端点接线：钉死版本走共享函数（替换现内联校验，
  语义含既有用例全量回归）
- [x] 3.3 corrections 端点可选 `artifact_id`：携带时验（本企业/kind=form/
  spec.datasetId=目标集）→ 当前版本走共享函数；不携带零漂移
- [x] 3.4 探测与选项解析的引擎调用上界测试（cell 档 ≤11 次/提交）

## 4. Web 构建器

- [x] 4.1 字段面板三新键配置：选项源（数据集下拉+列下拉+过滤+上限）、
  回填（源字段+列下拉）、计算（op=add_days、日期字段下拉、天数字段/字面量）
- [x] 4.2 spec 级约束编辑器（列表增删改：探测数据集/过滤条件（字面或
  {field}）/expect/message/when）
- [x] 4.3 `formSpec.ts` 草稿校验镜像扩展（三键+constraints 同规），
  非法配置前端即拦

## 5. Web 运行时（console 与公开页共用）

- [x] 5.1 FormRuntime 动态选项：console 走会话身份查询端点、公开页走
  选项端点；升序/截断/失效错误如实呈现
- [x] 5.2 选值回填：选项变化即查/查 fills 映射填默认值，可手改，重选覆盖
- [x] 5.3 计算字段：add_days 双端口径一致（闰年/月末），自动重算可手改
- [x] 5.4 提交闸前端预检（UX 层）+ 提交失败（服务端 422）如实呈现
  constraint message；console 提交带 artifact_id
- [x] 5.5 i18n zh/en 全量键补齐（构建器+运行时+错误文案）

## 6. 文档与验证

- [x] 6.1 DocsView「表单」章节扩写（四个新能力小节+公开选项面边界）zh/en
- [x] 6.2 后端 pytest：契约校验/选项解析（免费+cell 双档）/提交闸（none
  /any/when/引用字段代入/零漂移）/公开端点（令牌/限流/404/缓存）
- [x] 6.3 双端一致性用例表：同一组 spec+values 在 web 镜像函数与服务端
  共享函数的求值结果一致（选项解析/回填/computed/when）
- [x] 6.4 （**10-15 部署态实测通过**：platform-test 账号真链路；剧本在 reports/e2e-runbook.md，实测记录见 reports/e2e-results.md）E2E 手验剧本：投入品档案+打药/采收两表单（动态选项→回填→
  计算截止日→禁采闸拦截→间隔期外放行），console 与公开链接两路径各跑一遍
