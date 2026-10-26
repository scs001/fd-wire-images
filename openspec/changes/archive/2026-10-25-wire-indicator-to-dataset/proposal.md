# Proposal: wire-indicator-to-dataset

## Why

北极星承诺「客户的业务数据进来，第一天就能连上五万余个宏观/行业指标」，但目录今天只有「看」没有「取」：公开目录可搜索、详情页可浏览，`verified` 指标却没有产品化的拉数路径落成客户数据集——从「选中一个指标」到「它在 cell 里可 join、可画看板」之间是断缝，只能靠 AI 对话间接绕。这是护城河三件套第一件（指标目录即接即用 join）的最后一厘米。

## What Changes

- **建集第四态 `source.kind="indicator"`**：`POST /api/v1/datasets` 新增 indicator 来源——`{indicator_id, entity_type, entity_id, start, end}`；api 层经 MCP 调上游拉数（`business-mcp-indicator-series-read` change 新增的统一工具 `indicator_series_read`），落成单指标单集（date 主键 + value/unit 列）。
- **双档落地复用既有模式**：免费档（TIER_API）本地物化零引擎调用（照 `_create_from_upload_local` 模式，行数闸/存储闸照走）；cell 档走引擎 `ingest_begin → chunk → commit` 三段（照 `_create_from_upload` 模式，失败 abort 清理，计量照走）。
- **溯源留档**：数据集 `source_kind="indicator"`、`source_ref=indicator_id`；建集事件入溯源账本；行内 `brand=finddata` 由上游保证。
- **目录详情页导流升级**：verified 指标详情新增「落成数据集」入口（选实体 + 时间范围 → 建集跳详情页）；registered 指标维持既有需求单预填导流不变。
- **拉数零计费（产品口径，待用户推翻）**：自家数据免费 join 是护城河兑现方式；客户侧行数/存储照走既有配额闸；不改 marketplace 消费计量路径、不背 registry `rows_returned` 透传老账。
- **v0 边界**：单指标一集；多指标宽表/组合包留后续（可用管线/derive 拼）。

## Capabilities

### New Capabilities
- `indicator-to-dataset` — 指标目录到客户数据集的拉数建集通路（建集第四态、双档落地、溯源、导流入口）。

### Modified Capabilities
（无——公开目录与 registered 导流行为不变；catalog-governance 只读治理面不受影响。）

## Impact

- `api/fd_wire_api/datasets/routes.py`（create_dataset 分派 + 新建集函数）、`service.py`（键分配/配额照走）。
- 新增上游 MCP 客户端配置（business-mcp 端点 + 平台凭据；复用 `fd_mcp_client.NativeMcpClient` 范式）。
- web：目录详情页（portal 侧）与数据集列表（新建集来源徽标）；i18n 双语。
- 依赖：上游 change `business-mcp-indicator-series-read`（工作区根 openspec）先行或同批落地——wire 侧对该工具形状做契约对拍。
- 迁移：无新表（datasets 现有列够用）；`source_kind` 校验白名单扩一项。
