# Tasks: wire-console-resilience

## 1. 后端档位解析确定性

- [x] 1.1 `datasets/service.py`：新内部助手 `_resolve_customer_row`（全取
  → 空None/单行/多行按「sub 非空优先→id 最小」收敛）；`plan_code_for` 与
  `storage_limit_for` 接线
- [x] 1.2 pytest：孤儿行+真户同名 → 解析到真户；双孤儿行 → id 最小；
  单行零漂移；无行 None（既有用例回归）

## 2. 后端建档守卫与迁移

- [x] 2.1 `billing/customers.py` ensure_customer：查重条件 name 单键化
  （幂等返回既有行）；IntegrityError 兜底同步
- [x] 2.2 `admin/routes.py` 建档：同名（跨档）422 如实拒，零半写
- [x] 2.3 alembic 迁移：重名自愈改名（sub 非空保留 + `#orphan-<id>` 后缀）
  → `billing_customers.name` 唯一索引（SQLite batch）；down 只删索引
  （实施中抓真 bug：revision id 33 字符超 PG `alembic_version` varchar(32)
  → 缩短为 `0037_billing_customer_name_uq` + 全链长度守卫测试防复发）
- [x] 2.4 pytest：守卫矩阵（同档幂等/跨档幂等/admin 422）+ 迁移自愈用例
  （重名 fixture → 迁移 → 名称断言）

## 3. Web 配额条 null 防护

- [x] 3.1 QuotaBar（DatasetsPage）与 WorkbenchQuotaBar：`max_*` 任一 null
  → 中性提示行（不渲染数值条/CTA）；有值路径逐字零漂移
- [x] 3.2 i18n zh/en：`console.{datasets,workbench}.quota.unknown` 新键
- [x] 3.3 vitest：null 降级渲染 + 有值渲染快照/断言（两页各一）

## 4. 构建器状态重置修复

- [x] 4.1 定位：RecordViewEditor 多步交互状态丢失根因 = 渲染闭包
  `patchBody({...draft.body})` 覆盖未提交选择（非 remount）
- [x] 4.2 最小修复：草稿态单一来源，onChange 即写草稿
  （patchBody/toggleHeaderColumn → 函数式 setDraft）
- [x] 4.3 vitest：三步交互（选主数据集→选键列→加段选段数据集）后全部
  选择保持；FormEditor 同构排查（无同病灶）
- [x] 4.4 E2E 剧本 4.1 场景浏览器复验（生产滚版后）——2026-10-15 生产实测：
  新建单记录页 → 选主数据集（采收记录）→ 键列（地块编号）→ 勾头部字段 ×2
  → 加段（打药记录 + 记录日期）→ 保存 v2 → 试预览 A1 键值完整渲染
  （头部字段 + 打药记录三条记录）——全步选择零丢失

## 5. 文档与收尾

- [x] 5.1 free-tier-data spec 修订随代码同批（配额条降级语义已在 delta）
- [x] 5.2 全量验证：api pytest 全绿（1299 ×3）、web build+vitest 全绿（808）、
  ruff 通过
- [x] 5.3 生产滚版后浏览器复验（配额条正常 + 构建器多步走通）——
  2026-10-15 生产实测：配额条「免费档配额：已用 3/3 个数据集 · 每集
  ≤10,000 行」正常渲染（此前 null 崩页）；构建器多步全保留（见 4.4）
