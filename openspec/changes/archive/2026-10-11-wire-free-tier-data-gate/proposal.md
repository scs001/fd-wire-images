# wire-free-tier-data-gate

## Why

「我的数据」对免费档整页隐藏（`plan_code=free` → 升级墙），这是可发现性三断点之首，
也是 C 端信息工作台（docs/ROADMAP.md 方向 D）的起步前置——自助注册的个人天然是
免费档，今天进不来。架构根因不止 UI：数据集的一切读写都经 cell 引擎
（`ingest.EngineToolClient`），而免费档按 self-serve-onboarding 裁决**无 cell**，
所以"放行"必须先给免费档一条不依赖 cell 的数据存取路径。本 change 为数据集引入
**api 侧轻量存储层**（免费档）与既有 cell 物化层（付费档）的双层结构，同时为
方向 A 的 PG 化（wire-dedicated-postgres）打样——api 侧行存储从第一天起 SQLAlchemy
双方言。

## What Changes

- **数据集存储分层**：`storage_tier ∈ {api, cell}`。付费档默认 `cell`（现状行为
  零变化）；免费档建集落 `api` 层——api 侧通用行存储（列元数据 + JSON 行），
  不触碰 cell 引擎。
- **免费档限额**（配置项，默认值）：数据集 ≤3 个、每集 ≤10,000 行、仅上传通道
  （CSV）。连接器建集、derive/join 派生、批量写仍需 ≥基础档（这些路径都经 cell）。
- **免费档可用面**：上传 CSV、schema 预览、数据面 REST 查询/聚合（`wdk-` key，
  走 api 层本地执行）。MCP/AI 物化/派生仍是付费面。
- **计量口径不变（D7）**：客户数据集读取本就不走计量轨道，api 层同样不计行数
  额度；杠杆是数据集个数与行数上限，不是行数额度。
- **web 放行**：「我的数据」对免费档可见——列表/上传/配额余量内联呈现；升级
  引导从整页墙退位为限额触达（建集超限/行数超限）时的内联提示。
- **升级语义**：升档后既有 api 层数据集保持可用（不自动迁移）；迁移至 cell 物化
  是后续独立 change。

## Capabilities

### New Capabilities

- `free-tier-data`: 免费档轻量数据集——api 侧存储、上传通道、个数/行数限额、
  REST 查询聚合、档位闸（连接器/派生/批量写需付费档）。

### Modified Capabilities

- `self-serve-onboarding`: 免费档边界从「我的数据不可用并提示升级」改为
  「api 层轻量数据集可用 + 限额；cell 物化面仍需付费档」。

## Impact

- **fd-wire/api**：datasets 模块（tier 字段与迁移、api 层行存储引擎、上传按档分流、
  限额闸）、data_plane 查询/聚合执行路径（api 层本地分支）、billing 档位判断复用。
- **fd-wire/web**：DatasetsPage 放行与限额 UI、UploadPanel 免费档路径、i18n 双语。
- **不碰**：registry、cell-manager、连接器二进制、付费档既有行为（回归保障）。
- **伴生**：docs/ROADMAP.md 方向 D 首块砖；wire-dedicated-postgres 的双方言审计
  覆盖本 change 新增表。
