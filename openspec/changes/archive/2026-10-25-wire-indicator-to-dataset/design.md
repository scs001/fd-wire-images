# Design: wire-indicator-to-dataset

## Context

- 目录镜像在 `api/fd_wire_api/catalog/`（`indicator_id = source_db:native_code`，每日同步，不存观测值）。
- 拉数权威源是 business-mcp（33 工具）；观测值形状 `{date, value, unit, brand: "finddata"}`。
- 建集入口 `create_dataset` 按 `source.kind` 三态分派（upload/connector/declared），双档模式已验证：免费档本地物化、cell 档引擎三段 ingest。
- 上游统一拉数工具 `indicator_series_read` 由配套 change `business-mcp-indicator-series-read`（工作区根 openspec）新增；本 change 是它的消费方。

## Goals / Non-Goals

**Goals**: 选中 verified 指标 → 选实体与时间范围 → 一键落成可 join 的客户数据集；免费档与 cell 档都可用；溯源与配额零旁路。

**Non-Goals**:
- 多指标宽表、组合包、指标 Basket（留管线/derive 拼，v0 单指标一集）。
- marketplace 消费计量改造（拉数零计费口径下不需要）。
- 实时/增量拉数（v0 一次性快照建集；再拉 = 重建或后续增量 change）。
- registered 指标导流变更（维持需求单预填）。

## Decisions

### D1. ID 空间断层住在上游，wire 只做薄壳

目录 `indicator_id`（`source_db:native_code`）与上游各 read 工具的 native ID（如 `concept_ids: list[int]`）之间无统一映射。若 wire 侧解析，33 个 read 工具形状各异、每加一源 wire 改一处——**拉数解析逻辑必须住在 business-mcp**（它已按 source_db 组织 adapter）。wire 只传 `indicator_id + entity + range`，收统一形状的行。

**替代方案否决理由**：wire 侧按 source_db 适配 = 目录每接入一源就跨仓改一次，违背「目录越厚平台越值钱」的飞轮方向。

### D2. 建集分派复用 upload 双模式，不新建第三条落地路径

免费档：拉数 → 列推断（date/value/unit，全部 TEXT→按值推断沿用 `uploads.infer_type` 语义）→ 本地 SQLite 物化，行数闸（free_tier_max_rows 429）与存储闸照走。cell 档：引擎 `ingest_begin(pk=[date]) → chunk → commit`，失败 abort 清理，照 `_create_from_upload` 的事务边界。**api 侧行数闸先于拉数**：预估行数不可得（上游未返回 total），v0 以拉回后实际行数过闸——超闸整批拒绝零半写（免费档本地事务天然原子；cell 档 abort）。

### D3. 上游调用走平台凭据、出站-only，新增专用配置

新增 `INDICATOR_FETCH_URL` + `INDICATOR_FETCH_TOKEN`（缺省指向网内 business-mcp 副本，与 catalog settings 同款收敫模式）。**红线对账**：不碰客户源凭据（红线 4 云端零客户源凭据不受影响）；平台凭据调自家数据（红线 5 双凭据计量不适用——metering 不记此类调用，因拉数零计费）。

### D4. 重复建集 = 幂等拒绝 + 如实指引

同 customer + 同 `indicator_id` 已存在 `source_kind="indicator"` 活数据集 → 409，detail 带既有数据集 key（不自动覆盖、不自动刷新——刷新语义留给数据集既有 sync 体系，v0 不做）。改名建集（同名不同集）允许。

### D5. 溯源三锚点

`source_kind="indicator"` + `source_ref=indicator_id` + 溯源账本 `dataset.create` 事件 subject 携 `{kind: "indicator", indicator_id, entity, range}`。行内 `brand=finddata` 由上游保证（business-mcp spec 品牌隔离要求），wire 侧不做二次清洗但落库列保留 brand 值——证据链到行级。

## Risks / Trade-offs

- **[上游先行依赖]** wire 侧联调依赖 `indicator_series_read` 上线 → 两 change 同批出，wire 测试用假 MCP 客户端桩（对拍契约 fixture 固化，跨代理契约主线程对拍纪律）。
- **[大时间范围慢拉]** 冷区间同步拉数分钟级 → v0 前端如实文案「建集中」+ api 侧超时如实 502（不静默截断时间范围）；范围上限（如 30 年）校验拒绝。
- **[PG 方言雷区]** 免费档本地物化走 datasets 库（PG）——建表 DDL 与列类型照既有 api 档路径，不引入新方言分支。

## Migration Plan

无新表、无迁移。`source_kind` 字符串值扩一项，存量数据零影响（枚举校验处加项，默认 private 可见性照旧）。

## Open Questions

- 上游工具入参的 entity 表达（entity_type/entity_id 原样透传 or wire 侧给友好实体选择器字典）——待上游 change design 定稿后对齐（主线程对拍点）。
