# Design: wire-entity-layer

## Context

- 侦察事实（2026-10-12）：引擎 join=数据集×指标目录（`TOOL_JOIN_INDICATORS`，
  data_routes.py:380-419），**无数据集↔数据集 join 工具**；derive/join 均
  cell 档（`require_cell_tier` 403）；AI 构建系统提示=固定常量（loop.py:31-39），
  零企业上下文；自组 system 先例=refresh.py:146-149；工具描述内嵌契约先例=
  ai/tools.py:56-86；控制台加 tab 先例=packs（ConsoleLayout:21-49/App:47-60/
  ConsoleView:299-307）；数据集列快照=columns_json（两档建档/回填同写）。

## Goals / Non-Goals

见 proposal。核心取舍：**v0 不做物化语义 join**（引擎无工具，造假不如不做），
把语义层做成「声明+图谱+AI 上下文」的真地基；免费档全功能可用（零引擎调用）。

## Decisions

### D1. 数据模型与迁移（wire_platform 库，Alembic 0026）

- `entities`：id PK / customer_id / name（业务名，唯一约束 (customer_id,name)）/
  dataset_id（唯一约束 (customer_id,dataset_id)——一集一体）/ pk_columns JSON
  （非空字符串数组，语义层不自证类型）/ description TEXT 可空 / created_at /
  updated_at。**不存列快照副本**（列真相在 Dataset.columns_json，声明时校验、
  之后漂移不追改——与 form 的列纪律同口径）。
- `entity_relationships`：id / customer_id / from_entity_id / from_column /
  to_entity_id / to_column / kind（"n:1"|"1:1"）/ label 可空 / created_at；
  唯一约束 (from_entity_id, from_column, to_entity_id, to_column)；
  FK 到 entities（级联由 service 显式删，不依赖 DB CASCADE——SQLite/PG 双方言
  行为一致，照 corrections 惯例）。
- 迁移挂 wire_platform 链（0025 在 datasets 库链？——**注意双库链位**：0022
  arti/packs 在 platform 链、0025 在 datasets 链。实体现属 platform 库，revision
  down_revision 必须接 platform 链头（按 alembic_version 实况核对，照
  wire-source-packs 0022 的挂法）。

### D2. 校验规则（service 层纯函数+库校验）

- 实体：name 非空 ≤100 strip；dataset 存在+属本企业（datasets 库会话）；
  pk_columns 非空去重、每列 ∈ dataset.columns_json；重复 name→409、重复绑定
  数据集→409（资源冲突语义，区别于 422 结构错）。
- 关系：两端实体存在且同 customer；from_column ∈ 源实体 dataset.columns_json；
  **to_column ∈ 目标实体 pk_columns（纪律）**；kind 枚举；四元组唯一→409；
  自环（from==to 实体）v0 允许（自引用如「员工.manager_id→员工.id」是合法语义）。
- 删除实体：service 同事务先删关系再删实体；响应带 `deleted_relationships` 数量。

### D3. API 形状（新模块 `entities/`）

- `GET  /api/v1/entities` 列表（含 dataset 摘要：id/key/name/row_count/tier）
- `POST /api/v1/entities` →201；`PUT /api/v1/entities/{id}`（改名/主键/描述，
  重校验）；`DELETE /api/v1/entities/{id}`（级联关系）
- `POST /api/v1/entities/{id}/relationships` →201；
  `DELETE /api/v1/entities/{id}/relationships/{rel_id}`（rel 须以该实体为端点）
- `GET  /api/v1/entities/graph` →`{nodes:[{id,name,description,dataset:{id,key,
  name,row_count,storage_tier},pk_columns}],edges:[{id,from_entity_id,from_column,
  to_entity_id,to_column,kind,label}]}`
- 身份=console 会话（require_console_customer_id）；datasets 摘要走 datasets
  库会话（生产双库，照 form/artifacts 先例——**别合库夹具掩盖**）。
- 迁移外零引擎调用：免费档全功能。

### D4. AI 上下文注入（loop.py 最小侵入）

- `entities/context.py::entity_graph_prompt_block(db, customer_id) -> str | None`：
  图谱非空 → 组装紧凑文本块（先实体行「实体「名」=数据集 name（主键: a,b）」，
  后关系行「名.列 →(kind) 名.列」；≤50 实体 ≤100 关系，超限截断加注
  「（仅列前 50/100，完整图谱见控制台实体页）」）；空图谱 → None。
- `run_build_loop` 组 wire 时（loop.py:96 附近）：system = SYSTEM_PROMPT +
  （block or ""）——**block 为 None 时逐字节等于现状**（有测试断言）。
- 不改 TOOL_SCHEMAS、不改消息还原、不改 refresh 轮（无头刷新不需要业务语义，
  留待确需时）。

### D5. Web 形态

- ConsoleLayout 新 tab `entities`（labelKey console.nav.entities，zh=实体
  en=Entities）；App.tsx parseConsoleTab 白名单+ConsoleView 条件渲染。
- `web/src/console/EntitiesPage.tsx`：三区——实体卡列表（业务名/数据集名/
  主键徽标/描述/删除）、创建表单（数据集下拉→主键列多选勾选（列来自
  GET /datasets/{id} 或列表 columns）→业务名→描述）、关系管理（每实体展开：
  关系行+添加表单（目标实体下拉=其他实体、源列=本集列、目标列=目标主键、
  kind 二选一））；SVG 小图谱（节点圆角矩形+边线+kind 标注，节点点击滚到
  实体卡——照 PipelineView 连线风格轻实现）。
- api client：entities CRUD + graph + 复用 fetchDatasets。
- i18n zh/en 全键；文档「实体」章节（docs.entities.*，3-4 步：声明→关系→
  AI 带图谱→v1 边界说明）。

### D6. 红线对照

- 图谱端点 console 身份专属（401 兜底）；实体元数据不含行数据（列名/主键属
  客户自声明面，console 域内）。AI 注入走平台侧 system prompt（企业域内容进
  企业会话，无跨域面）。零引擎调用=零 cell 面。

## Risks / Trade-offs

- **声明不物化**（v0 无 linked dataset）——客户预期管理靠文档如实说明 v1 边界；
  AI 上下文注入是当下可感知的真收益。
- 列漂移：实体 pk_columns 不随数据集列演进自动校验改写——图谱/AI 注入用声明
  原样，漂移由 AI 使用时报错暴露（与 form 提交时裁决同口径）。
- 双库链位分叉（platform@002X/datasets@0025）——迁移 down_revision 挂错库链
  会静默建错库，实现时按生产 alembic_version 实况核对（既有坑）。

## Migration Plan

单迁移 0026（platform 库链）两张表；无数据回填；回滚=drop 两表。部署顺序=
先滚 api（迁移随部署链跑）后滚 web（页面在旧 api 上 404 图谱端点会如实报错
——web 发布与 api 同批即可，ArgoCD 双部署同滚）。

## Open Questions

无——物化 join/本体深化/管线整合均明示非目标另立。
