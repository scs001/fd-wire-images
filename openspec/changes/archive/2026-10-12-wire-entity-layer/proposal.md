# Proposal: wire-entity-layer — 轻量实体语义层 v0（实体+关系声明+图谱进 AI）

## Why

北极星（类 Palantir 数据中台）关键缺件按重要性排第一的就是**实体语义层**：
方向 A 明确「轻量实体建模（实体+主键互链+关系声明），join 从表连接升为业务
语义连接——中台化绕不开，但不必做全量本体」。现状是客户数据侧只有「表」：
AI 构建的 SYSTEM_PROMPT 零企业上下文（loop.py 固定文案），AI 与人都不知道
「订单.customer_id 就是客户.id」这类业务语义；跨表关系只存在于人脑里。

侦察确认：引擎的 join 是「数据集×指标目录」连接，**不存在数据集↔数据集
join**（需引擎新工具，属 v1 物化）；因此 v0 落**语义声明层本体**——把实体与
关系变成平台一等元数据，图谱注入 AI 构建上下文（AI 带业务语义选列/解释连接/
正确用指标 join），并给控制台管理面。这是后续语义物化 join（v1）与本体深化的地基。

## What Changes

- **实体声明**：客户域一等元数据——实体 = 业务名 + 绑定数据集 + 主键列（非空、
  ⊆数据集列）+ 可选描述；每数据集至多一个实体声明；新表 `entities`
  （wire_platform 库，Alembic 0026）。
- **关系声明**：实体字段 → 目标实体**主键列**的有向关系（kind: n:1|1:1）；
  新表 `entity_relationships`；from 列须 ∈ 源实体数据集列、to 列须 ∈ 目标
  实体主键（语义纪律：关系指向身份）；删除实体级联删其关系。
- **图谱端点**：`GET /api/v1/entities/graph`（nodes=实体+数据集摘要，
  edges=关系）——控制台与 AI 上下文同源消费。
- **AI 构建上下文注入**：run_build_loop 组装 system prompt 时，实体图谱
  非空则追加紧凑文本块（实体=数据集+主键、关系指向），有界（≤50 实体/≤100
  关系截断如实标注）——照 refresh.py 自组 system 先例，零工具 schema 改动；
  图谱为空零注入零漂移。
- **控制台「实体」页**：侧栏新 tab——实体列表/创建（数据集选择+主键列勾选+
  业务名）、关系管理（实体对+列+基数）、SVG 小图谱；i18n zh/en。
- **文档双语教程**（红线⑥）：声明实体→建关系→AI 构建带图谱上下文（同批上线）。

## Impact

- **specs**：新能力 `entity-layer`（声明契约/关系纪律/图谱/AI 注入/控制台面）。
- **api**：新模块 `entities/`（models+routes+service）+ 迁移 0026（wire_platform）；
  `ai/loop.py` system prompt 组装扩展（图谱非空才注入）。
- **web**：ConsoleLayout 新 tab、EntitiesPage、App 路由、i18n、文档章节。
- **兼容**：纯新增；derive/join/管线/产物路径零改动；免费档可声明实体（零
  引擎调用），语义物化 join 明确非目标（v1 引擎事）。

## Non-Goals

- **语义物化 join（linked dataset）**：数据集↔数据集 join 需引擎新工具
  （customer_dataset_link），v1 另立 change（含旧 cell 兼容）。
- 本体属性/类型层级/跨实体继承（不做全量本体）。
- 关系上的基数推理/自动外键发现（n:m 建模、列相似度推断等）。
- 实体进管线视图（管线=数据集血缘域，图谱页独立 v0）。
