# Design: wire-free-tier-data-gate

## Context

数据集现状：上传与连接器推送都经 `ingest.EngineToolClient` 物化进客户 cell；免费档
无 cell（self-serve-onboarding 裁决），因此 web 侧以升级墙整页隐藏「我的数据」。
C 端方向（ROADMAP 方向 D）要求个人用户第一天就能上传/查询自己的数据；方向 A 的
PG 化要求 api 侧存储从第一天起双方言。术语见 `fd-wire/CONTEXT.md`。

```
免费档（无 cell）                        付费档（有 cell）
上传 CSV ──> api 层行存储 ──> REST 查询/聚合   上传/连接器 ──> 引擎物化（现状不变）
（列元数据 + JSON 行，SQLAlchemy 双方言）      REST/MCP/AI 派生（现状不变）
```

## Goals / Non-Goals

**Goals:**
- 免费档在「我的数据」可上传 CSV、预览 schema、REST 查询/聚合自己的数据
- 限额为配置项（默认 3 集 × 10,000 行），触达时内联提示升档，非整页墙
- 付费档行为零回归；api 侧行存储 SQLite/PG 双方言
- 档位闸诚实呈现：连接器/派生/批量写/MCP 在免费档明确标注需付费档

**Non-Goals:**
- api 层数据集的 derive/join/批量写（这些语义与引擎物化耦合，付费面）
- 免费档数据集迁 cell 物化（升档后自动迁移=后续 change）
- 连接器对免费档开放（连接器推送经引擎，属付费面）
- 源包订阅/个人工作台（方向 D 其余部分独立 change）

## Decisions

- **D1 tier 由档位决定建集时刻**：建集时按客户 plan 写入 `storage_tier`
  （免费=api、付费=cell）；存量数据集不回填（付费存量全是 cell 层）。升档不迁移，
  免费期建的 api 层数据集升档后继续可用。
- **D2 api 层存储形态**：datasets 库内两张新表——`dataset_columns`
  （dataset_id、name、dtype、ordinal）与 `dataset_rows`（dataset_id、row_index、
  data JSON）。窄查询面：过滤（eq/in/gt/gte/lt/lte/contains/startswith）、排序、
  分页、count、distinct、聚合（group by ≤3 列 × count/sum/avg/min/max）。JSON 取值
  经 SQLAlchemy JSON path，双方言兼容；数值聚合列在写入时按 dtype 归一。
- **D3 限额两闸**：建集闸（个数）与写入闸（行数），都在 api 层强制，错误码带
  余量与上限；默认值 env 可覆盖（`FD_WIRE_FREE_MAX_DATASETS` /
  `FD_WIRE_FREE_MAX_ROWS`），不进 billing 表（档位含义变化时再结构化）。
- **D4 上传按档分流**：UploadPanel 后端在接收时判定 tier——cell 层走既有
  ingest 链路；api 层走新存储（CSV 解析与类型推断复用既有上传代码，若解析与
  引擎耦合则把解析部分抽为共享函数，不复制实现）。
- **D5 读取不计额**：api 层数据集的 REST 查询/聚合不走 metering_records
  （与 D7 口径一致：客户数据读取不过计量轨道）；限流沿用数据面既有 rpm。
- **D6 web 放行形态**：免费档 DatasetsPage 渲染完整页面；配额条内联显示
  「已用 X/3 集、每集 ≤10,000 行」；连接器区与派生入口可见但标注付费，点击给
  升级说明（不是隐藏——可见性正是本 change 的目的）。

## Risks / Trade-offs

- [api 层查询面窄被吐槽] → 如实标注免费面与付费面差异；查询面扩展随需求增量
- [JSON 行存储在 10k 行规模性能可接受，超限是商业升档杠杆] → 不做分块优化
- [免费档滥用（批量注册刷存储）] → 个数×行数上限硬闸 + 既有 suspend/key 吊销治理；
  上传体积单件上限沿用既有配置
- [tier 字段与既有 source 字段（upload/connector）正交] → source 是"从哪来"，
  tier 是"存哪里"，互不替代

## Migration Plan

1. 迁移：datasets 数据集表加 `storage_tier`（默认 cell，存量不回填）+ 两张 api 层
   存储表；drift check 通过
2. api：存储引擎（写/查/聚合）+ 上传分流 + 限额闸 + 数据面执行分支
3. web：放行与限额 UI + i18n 双语
4. 测试：免费路径全绿 + 付费路径回归全绿 + 双方言建表冒烟

## Open Questions

- 免费档数据集是否出现在数据面 lineage/derive 的依赖提示中（标记付费）——实施时
  按最小呈现处理
