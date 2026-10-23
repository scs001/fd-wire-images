# Tasks: wire-entity-layer

## 1. api — 模型与迁移

- [x] 1.1 `entities/models.py`：Entity/EntityRelationship ORM（唯一约束见 design
  D1；不依赖 DB CASCADE，级联走 service）；迁移 0026 挂 **wire_platform 链**
  （down_revision 按生产 alembic_version 实况核对，照 0022 先例）
- [x] 1.2 迁移可升可降（upgrade/downgrade 双方言），本地 sqlite 冒烟 + 干跑

## 2. api — service 与路由

- [x] 2.1 `entities/service.py`：实体 CRUD 校验（name/dataset 属主/pk ⊆ 列快照/
  唯一 409）、关系 CRUD（目标列 ∈ 目标主键纪律/四元组 409/级联删）、
  `entity_graph_prompt_block` 纯组装（有界截断）
- [x] 2.2 `entities/routes.py`：六端点（列表/建/改/删/关系建删/图谱；console
  身份；datasets 摘要走 datasets 库会话）；错误码矩阵 404/409/422
- [x] 2.3 `ai/loop.py`：system prompt 组装扩展（block 非空追加；空=逐字现状）
- [x] 2.4 pytest：校验矩阵/隔离/级联/图谱形状/AI 注入三态（有图谱含块、空图谱
  逐字一致、超限截断标注）+ 免费档全程零引擎断言

## 3. web — 实体页与文档

- [x] 3.1 api client（entities CRUD+graph）+ ConsoleLayout tab + App 路由 +
  ConsoleView 挂载 + i18n zh/en
- [x] 3.2 `EntitiesPage.tsx`：实体卡列表+创建表单（数据集下拉→主键列勾选）+
  关系管理（目标实体/源列/目标主键/kind 表单纪律）+ SVG 小图谱
- [x] 3.3 文档「实体」双语章节（含 v1 语义 join 边界如实说明）
- [x] 3.4 vitest：页面三区/创建流/关系纪律下拉/AI 相关零改（既有 ai 测试不回归）
  + i18nKeys 完整性 + tsc 零错

## 4. 验证与发布（主线程）

- [x] 4.1 门禁：api pytest 全绿 / web vitest+tsc 零错 / `openspec validate
  wire-entity-layer --strict` 零 error
- [x] 4.2 发布：出片走 law-ai-official 快照仓通道（用户 10-11 既定；失败回退
  cheap-3 手工构建桥）→ 镜像到位 → manifest 滚版 → ArgoCD → 迁移上生产 PG
  核对（platform 库 alembic_version=0026）
- [x] 4.3 公网冒烟：建实体（免费档）→建关系→图谱→AI 对话含图谱块→删除级联；
  归档 change，主 spec 合并核对
