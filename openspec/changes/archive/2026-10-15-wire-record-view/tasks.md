# Tasks: wire-record-view

## 1. 后端产物契约

- [x] 1.1 `artifacts/service.py`：RECORD_VIEW_KIND 常量、BLANK_CREATE_KINDS
  追加、`_blank_spec` 分支（最小空 spec）、`validate_kind_spec` 分派
- [x] 1.2 新 `artifacts/record_view_spec.py`（或并入 form_spec 同层）：
  信封/headerFields/sections 形状校验（列快照内、fromField 引用主记录列、
  ≤50/≤10/≤10 边界、limit 1..500）、属主校验接线（他企/不存在 422 零半写）
- [x] 1.3 展示面收录：`workbench.py` WORKBENCH_KINDS + web api.ts 展示序
  两处一致追加；AI 门确认不收录

## 2. 分享与长效令牌

- [x] 2.1 `share/routes.py`：ALLOWED_TTL_DAYS 追加 0、SHARE_KIND_PATHS
  追加 record-view；生成面 0 档响应 expires_at=null
- [x] 2.2 `share/service.py`：`verify_token` exp==0 跳过过期校验（其余
  校验零变化）；生成/验签双向用例（0 档/正档/非法档）
- [x] 2.3 既有 dashboard/form 分享用例全量回归（缺省 30 天行为零漂移）

## 3. 公开取数端点

- [x] 3.1 `share/routes.py`：`GET /api/v1/share/record-view/{id}?t&k` —
  验签+钉死版本+503 闸+统一 404；主记录等值查（limit 1）零行 found:false
- [x] 3.2 sections 组装：`{fromField}` 代入、升序、limit、单段失败 error
  标注其余照常（复用 `_query_dataset` tier 分流）
- [x] 3.3 限流（缺省 240/h，env FD_WIRE_SHARE_RECORDVIEW_RATE_PER_HOUR）
  + 60s 短缓存（键=产物/版本/令牌 mac/k）
- [x] 3.4 pytest：取数/无此记录/引用失效/限流/缓存命中/404 形态/免费与
  cell 双档

## 4. Web 构建器与详情面

- [x] 4.1 RecordViewEditor：主数据集+键列选择、headerFields 勾选与 label
  改写、sections 配置（数据集/日期列/filters 字面或引用主记录字段/上限/
  标题）、草稿校验前端镜像
- [x] 4.2 示例键值试预览（console 身份同一组装逻辑渲染，不生成链接）
- [x] 4.3 详情面分享+二维码：键值选择器（keyColumn 去重下拉≤500 或手输）、
  URL 展示、QR 库懒加载、PNG 高清/SVG 下载、0 档印刷物提示

## 5. 公开页渲染

- [x] 5.1 ShareView 第三 mode（record-view）：裸 fetch 取数、四态（加载/
  就绪/无此记录/无效链接）、noindex 与品牌面复用
- [x] 5.2 RecordViewRuntime 时间轴：移动优先布局、头部字段区、按日期升序
  节点、节点点击展开行详情
- [x] 5.3 i18n zh/en 全量键补齐

## 6. 文档与 E2E

- [x] 6.1 DocsView 新「单记录公开页」章节（zh/en）：构建/分享/二维码/
  公开读面暴露边界（令牌门粒度、k 可枚举、撤销语义）如实说明
- [x] 6.2 （**10-15 部署态实测通过**：platform-test 账号真链路；剧本在 reports/e2e-runbook.md，实测记录见 reports/e2e-results.md）E2E 手验剧本：批次集+四类农事记录集 → 建页 → 发布 → 0 档令牌
  → 出二维码 → 扫码（手机）看时间轴 → 补录一条打药 → 60s 后扫码可见 →
  归档后扫码 404
