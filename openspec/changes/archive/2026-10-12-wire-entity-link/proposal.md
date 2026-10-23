# Proposal: wire-entity-link — 关系物化：数据集间语义连接（entity-layer v1）

## Why

wire-entity-layer v0（当日已上产）落了实体/关系**声明层**与图谱进 AI 上下文，
但「join 从表连接升为业务语义连接」的本体还差最后一块：引擎只有
`derive`（单父查询物化）与 `join_indicators`（数据集×指标目录），
**没有数据集↔数据集 join 工具**——客户声明了「订单.customer_id →(n:1)
客户.id」后，仍要手工搬数才能得到连接视图。本 change 补上引擎 link 工具与
「按关系声明一键物化」的端到端链路，entity-layer 从元数据层长成可执行的
语义层。

## What Changes

- **引擎新工具 `customer_dataset_link`**（DAAS 仓 internal-main）：LEFT JOIN
  语义（左表全保留、右列未命中置 NULL——沿用「绝不取邻值」纪律）；右列与
  左列撞名加 `right_` 前缀；子集主键=左集主键；行数超限整批拒绝（子集保持
  原状）；血缘记账 kind="link"（parent=左集，spec 携带右集四元组）；
  `derive_refresh` 增加 link 分发（重物化=按记录的 spec 重连）。
- **api 物化端点**：`POST /api/v1/entities/{id}/relationships/{rel_id}/
  materialize`（console 身份）——按关系声明解析两侧数据集与列，两端均须
  cell 档（免费档 403 档位闸如实）；子集登记 source_kind="derived"、
  source_ref=左集 key（管线图自然挂上左父边）；名称缺省
  「左实体名×右实体名 连接」；旧引擎 cell（未含 link 工具）返回如实错误。
- **web**：关系行新增「物化连接」动作（两端 cell 档才显示）——成功提示新
  数据集名并可跳「我的数据」。
- **文档**（红线⑥）：实体教程补「物化连接」一节（含 LEFT JOIN 语义与
  撞名前缀口径、旧 cell 滚动窗口说明）。

## Impact

- **specs**：`entity-layer` ADDED「关系物化：数据集间语义连接」。
- **引擎**（fd-daas-mcp internal-main）：customer_dataset_tools.py 新工具
  +refresh 分发 + 测试；cell 镜像随快照仓重建自动带上。
- **api**：ingest 常量 + entities 路由新端点（tier 双闸+子集登记+列/体量
  回填）；零迁移。
- **web**：关系行动作 + i18n + 教程。
- **兼容**：纯新增；derive/join_indicators/corrections 零改动；免费档实体
  声明与图谱功能不受影响（物化是 cell 档能力，与 derive/join 同级）。

## Non-Goals

- 多关系组合 join/跨链自动物化（一次一条关系，链式=对连接结果再建实体再连）。
- join 类型选择（INNER/RIGHT）与聚合语义（v1 固定 LEFT JOIN）。
- 连接结果的自动重物化调度（复用既有派生定时面，derive_refresh 语义已覆盖
  手动/定时入口——定时登记走既有 derived_dataset 调度即可，不在本 change 扩 UI）。
- 血缘图双亲边（v1 管线边=左父，右父在 spec 与实体页可见）。
