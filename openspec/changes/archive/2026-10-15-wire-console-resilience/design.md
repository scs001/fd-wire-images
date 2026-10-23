# Design: wire-console-resilience

## Context

- 生产事故链（2026-10-15 E2E 实测）：孤儿行（admin 手工建档，`logto_sub`
  为 NULL）与自开户真户同 `name` → `plan_code_for`/`storage_limit_for`
  （`datasets/service.py:146/:291`）`scalar_one_or_none` 抛
  MultipleResultsFound → `except Exception` 吞成 None → 按未知档 fail-closed
  到 cell → 建集走引擎 403 + quota `max_*=null` → 前端 QuotaBar
  `null.toLocaleString()` 炸 ErrorBoundary。当日手工改名孤儿行止血
  （`#orphan-std` 后缀）。
- 建档路径：`ensure_customer`（`billing/customers.py:39`，自开户幂等键按
  name+plan 派生，查重条件含 plan_id → 跨档同名不拦）与 admin 建档
  （`admin/routes.py:214` 同样含 plan_id 条件）。`logto_sub` 列 unique，
  name 无约束。
- 前端配额条两处：`DatasetsPage.tsx:531`（QuotaBar）、
  `WorkbenchPage.tsx:131`（WorkbenchQuotaBar）；i18n 键
  `console.{datasets,workbench}.quota.bar`。
- 构建器状态重置：E2E 复现——RecordViewEditor 多步交互后主数据集/键列
  选择被清空。待实施时定位（嫌疑：段数据集 change 处理器里的 normalize
  回写、或列表 key 不稳定导致受控组件 remount）。

## Goals / Non-Goals

**Goals:** 档位解析永不抛多行异常；建档守卫杜绝再造重名；配额条对
fail-closed null 如实降级；构建器多步配置一次走通。

**Non-Goals:** 身份模型重构（sub 主键化）、admin UI 重做、构建器重写
（proposal 已列）。

## Decisions

**D1 解析确定性规则：sub 非空优先 → id 最小。**
`plan_code_for`/`storage_limit_for` 共用一个新的内部解析助手
`_resolve_customer_row(session, name)`：`.where(name == …)` 全取
（`scalars().all()`），空→None、单行→该行、多行→按 `(sub 为空, id)`
排序取首（`sub 非空`排前，再按 id 升序）。规则与生产手工止血一致
（真户恒有 sub）。语义为纯读解析，不写库不迁移数据。

**D2 唯一约束迁移：先自愈后建索引。**
新 alembic 迁移：① 按 D1 同规则扫描重名组——组内保留 sub 非空（有则 id
最小）一行原名，其余行 `name = name || '#orphan-' || id`（与生产止血同
款后缀；`#` 非法入 email/命名空间键的场景不存在——name 是显示与键名用
自由串）；② `CREATE UNIQUE INDEX uq_billing_customers_name`。SQLite 走
batch 约束（照 0033 先例）。down 迁移只删索引不改名（改名不可逆如实）。

**D3 建档守卫分路径。**
`ensure_customer`：查重条件从 `(name, plan_id)` 放宽为 `name`——命中即
返回既有行（幂等语义不变；跨档同名复用，`logto_sub` 由 onboarding 终态
步骤写入）。IntegrityError 兜底分支同改。admin 建档（`admin/routes.py`）：
查同名（不含 plan_id 条件）命中即 422「客户名已存在」。两处守卫都在
唯一索引之前形成行为防线（索引是最后防线）。

**D4 配额条 null 防护=降级提示，不改契约。**
`max_datasets`/`max_rows_per_dataset` 任一为 null → 渲染中性行
（新 i18n 键 `console.{datasets,workbench}.quota.unknown`：
「当前档位限额信息不可用——如需确认配额请联系我们。」，en 同步），
不渲染数值条与升档 CTA（数值未知时 CTA 语义不成立）；两页共用判定。
数据侧不改（null 是既有 fail-closed 契约，free-tier-data spec 已修订）。

**D5 构建器重置修复：先定位后最小修。**
实施顺序：重现（三步交互）→ 定位（嫌疑：段数据集 onChange 里的 spec
normalize 回写清了未「提交」的字段选择；或 React 列表 key 用 index 导致
段增删时受控 select remount）→ 最小修（草稿态统一从单一份额 state 派生，
onChange 立即写入草稿而非等保存）。修复判据=E2E 剧本 4.1 场景纯 UI 走通
+ 新增 vitest 用例（多步交互后草稿值保持）。

## Risks / Trade-offs

- 改名迁移对「故意同名」的存量场景是行为变更——但该场景本身就是缺陷
  （解析已不确定），迁移注释如实说明。
- admin 建档 422 对既有自动化（若有脚本批量建户同名跨档）是收紧——
  生产当前唯一重名即孤儿事故行，无合法用例。
- 构建器修复在定位前无法精确估计范围；若定位发现是更深的状态管理问题，
  按 tasks 的挂起点回报而不扩散重构。
