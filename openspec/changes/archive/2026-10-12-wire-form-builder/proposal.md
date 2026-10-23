# Proposal: wire-form-builder — 表单构建器与运行时（方向 B 第三支柱）

## Why

方向 B（应用平台层）的缺件序列是：产物转正 ✓ → 看板渲染+编辑器 ✓ →
**表单构建器/运行时 ← 本 change** → 签名链接分享 ✓（已提前做完）→ 团队空间
（二期）。看板线已通（AI 生成→渲染→编辑→刷新→分享），但「用可修改的表单页
记录/处理数据、做成成熟 web 直接分享给别人填」还完全缺位——客户的数据进来后
只能"看"，不能低成本"收"（一线人员录入、外部访客登记、周期性上报）。

后端地基大半现成：表单写的语义 = 既有 corrections 覆盖层（带审计、可回滚、
永不写回客户源）；artifacts 版本链与发布状态机在役；分享令牌机器
（HMAC 钉版本）只差放行 form kind。

## What Changes

- **新产物 kind `form`**：spec 信封 `{title, spec:{datasetId, fields[],
  submitLabel?, successMessage?}}`；field = `{column, label, input:
  text|number|date|select, required, options?}`。空白创建白名单从
  dashboard 扩为 `(dashboard, form)`——form 与 dashboard 同属结构简单、
  可空白出生的 kind（其余四类仍只能 AI 草稿转正）。
- **表单构建器（console）**：产物页「新建表单」→ 选数据集（本企业列表）→
  列勾选（按数据集列序自动成 field，可改 label/input/required/select 选项）→
  submit 文案 → 保存走既有 PUT 版本链。
- **表单运行时（console）**：form 详情内嵌渲染——提交 = 既有 corrections
  端点 `op=insert`（api 档本地层/cell 档引擎透传，actor=`console:<cid>`，
  免费档行数闸照常生效），成功显示 successMessage。
- **表单分享与公开提交**：`POST /api/v1/artifacts/{id}/share` 放行
  kind=form（链接路径 `/api/v1/share/form/{id}`，令牌机器零改动）；公开
  `GET /api/v1/share/form/{id}?t&v` 返回钉死版本 spec（只含字段定义，零
  行数据）；公开 `POST /api/v1/share/form/{id}/submit?t&v` 提交一条
  insert 修正——按钉死版本 spec 校验（未知列拒、必填拒、number/date/select
  类型校验），tier 路由同 corrections（api 档零引擎、cell 档引擎），actor
  =`share-form:{artifact_id}`（审计可归因），令牌级滑动窗限流（缺省 60/小时，
  env 可调）防公开滥用。
- **文档双语教程**（红线⑥ 能力上线同批配文档）：web 文档页新增「表单」
  章节（创建/绑定/试填/分享全流程 zh/en）。

## Impact

- **specs**：新能力 `form-apps`（表单契约/构建器/运行时/分享提交四组要求）；
  `artifact-lifecycle` ADDED「form kind 空白创建与 spec 校验」。
- **api**：artifacts（白名单+form spec 校验+初始 spec）、share（form 链接+
  公开 GET/POST submit+限流器）；datasets corrections 零改动（复用）。
- **web**：console（New form 入口/FormEditor/FormRuntime/详情展开）、
  ShareView form 态、i18n、文档章节。
- **兼容**：纯新增；dashboard 分享/空白建板语义零漂移；既有五类 AI 产物
  kind 不动（form 不入 AI 工具白名单——v0 非目标）。

## Non-Goals

- AI 构建产 form（AI 工具指令不动；form 由 console 构建器出生）。
- update/delete 型表单（v0 只做 insert 录入面；编辑既有行走数据集详情的
  行内编辑器，已上线）。
- 团队空间/子域托管形态（远期）；公开提交的验证码/邮件确认（防滥用 v0 靠
  令牌限流+corrections 可回滚）。
- 表单字段联动/条件显隐（v1+）。
