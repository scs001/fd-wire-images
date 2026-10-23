# Proposal: wire-console-resilience（控制台韧性三修：配额面板 null 防护 + 档位解析确定性 + 建档同名守卫）

## Why

2026-10-15 生产 E2E（wire-form-entry-advanced/wire-record-view 部署态）实测
抓出一组连环故障，暴露三条韧性缺陷：

1. **quota 面板 null 防护缺失**：`GET /v1/datasets/quota` 对档位未知客户返回
   `max_*: null`（既有 fail-closed 契约），但「我的数据」页 QuotaBar 与
   工作台 WorkbenchQuotaBar 直接 `.toLocaleString()`——null 即整块
   ErrorBoundary（wire-console-ux-fixes 引入时假设恒有值）。
2. **档位解析多行不确定**：`billing_customers.name` 无唯一约束。生产实测
   一行无 `logto_sub` 的孤儿行（后台手工建档遗留）与自开户真户同名 →
   `plan_code_for`/`storage_limit_for` 的 `scalar_one_or_none` 撞
   MultipleResultsFound → except 吞成 None → 客户被 fail-closed 到 cell 档
   → 建集 403、quota null，整条免费链瘫。当日以改名孤儿行止血，但**同一
   事故可被再造**（后台建档不受 `ensure_customer` 唯一性约束路径管）。
3. **构建器多步配置状态重置**：单记录页/表单构建器连续交互时，先前步骤
   已选的键列/主数据集会被后续交互触发的重渲染清空（生产 E2E 复现一次，
   绕道 API PUT 才完成配置）。

## What Changes

- **Web 配额条 null 防护**：QuotaBar/WorkbenchQuotaBar 对 `max_datasets`/
  `max_rows_per_dataset` 为 null/undefined 时按「档位信息不可用」降级——
  渲染中性文案行（如实：无法确定当前档位限额），绝不抛错；有值时行为
  逐字不变。
- **档位解析确定性（后端）**：`plan_code_for`/`storage_limit_for` 的客户
  行查询改为**确定性单行解析**——同名多行时优先取 `logto_sub` 非空的行
  （自开户户恒有 sub，孤儿行恒无），仍有歧义取 `id` 最小者；多行且全无
  sub 才 None（fail-closed 原方向不变）。查询不再可能抛 MultipleResultsFound。
- **建档同名守卫（后端）**：`ensure_customer`（自开户路径）与 admin 建档
  在创建前显式查同名行：存在同名即幂等返回/422 如实拒，不再可能造出第二个
  同名行； alembic 迁移给 `billing_customers.name` 加唯一索引（存量重名行
  迁移前先按「sub 非空优先」规则自动改名孤儿行——迁移内自愈）。
- **构建器状态重置修复（web）**：定位单记录页构建器多步交互的状态丢失
  根因并修复（疑似受控 select 的 key/remount 或草稿态被归一化清写），
  修复后 E2E 剧本 4.1 的纯 UI 多步配置可一次走通。
- 文档与红线：free-tier-data spec 修订「限额呈现」条款（档位未知时的
  降级语义）；console-auth 不动。

## Impact

- 代码：`web/src/console/{DatasetsPage,WorkbenchPage}.tsx`（null 防护）、
  `web/src/console/recordview/RecordViewEditor.tsx`（状态重置修复）、
  `api/fd_wire_api/datasets/service.py`（plan 解析）、
  `api/fd_wire_api/billing/customers.py` + `api/fd_wire_api/admin/routes.py`
  （建档守卫）、alembic 新迁移（唯一索引+孤儿行自愈改名）。
- 兼容：quota 正常路径零漂移（有值时渲染逐字不变）；plan 解析单行场景
  行为不变；`ensure_customer` 幂等语义不变（同名同档已有幂等返回，本修
  只是跨档同名也拦）。
- 风险：唯一索引迁移在重名存量上会失败——迁移内先自愈改名再建索引；
  改名规则（`#orphan-<id>` 后缀）与生产手工止血一致。

## Non-Goals

- **admin 后台建档 UI 重做**：守卫在 service 层，后台 UI 形态不动。
- **customer 身份模型重构**（name 唯一 vs sub 唯一之争）：v1 保持 name
  命名空间键语义，只加确定性解析+守卫；sub 主键化另立。
- **构建器整体重构**：只修状态丢失缺陷，不重写表单/记录页编辑器。
