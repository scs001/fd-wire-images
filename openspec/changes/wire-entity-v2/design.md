# wire-entity-v2 — Design

## D1 属性元数据存 entities 表新列，不建新表

`entities` 增 `display_column`（String，nullable）与 `column_notes`（JSON_DUAL_DIALECT，
nullable）两列——实体与数据集本就一比一，独立属性表是过度设计。列快照校验沿
既有主键列校验路径（保存时刻校验、漂移在使用处如实）。Alembic 迁移双方言对拍。

## D2 关系 kind 扩展：1:n 是视图、m:n 才是新存储语义

- `1:n`：纯反向声明——落库时**归一化存储为 n:1**（源列/目标列对调、kind=n:1）+
  保留声明方向标记（`declared_kind` 列），遍历与图谱按声明方向呈现。这样关系
  存储形状不变、物化端点与既有查询零改动，只有展示层按声明方向翻转。
- `m:n`：新增 `junction_dataset_id` + `junction_source_column` +
  `junction_target_column` 三列（nullable，仅 m:n 用）。遍历=两段查询
  （中间集按源主键过滤取目标键集合 → 目标集主键 in 查询）。
- 互斥校验：同四元组正向/反向查重按归一化后形状（1:n 声明转 n:1 后查重，
  与既有 409 语义自然合并）。

## D3 对象查询复用引擎既有工具，零新引擎工具

对象列表/详情/遍历全部翻译为既有引擎查询工具（customer_dataset_query 语义：
filters/sort/limit/columns）——detail=主键等值 filters；n:1 反查=目标主键 in；
m:n=中间集查询 + 目标集 in。**引擎零改动**，cell 镜像无需滚动。limit 钳制
（列表 ≤200/遍历 ≤200，沿 record-view 的钳制纪律）。

## D4 MCP 三工具薄壳直调端点

wire-ops-mcp 的 `wire_object_*` 三工具与 console 三端点共享同一 service 函数
（沿 wire-ops 既有范式：工具=端点的 MCP 壳）。鉴权差量只有一点：viewer 经
MCP 只读本就允许（对象工具全只读，无 viewer 拒绝面——与 wire_action_propose
相反，无需角色门）。

## D5 AI 注入块格式

紧凑文本行扩展为：`名=数据集（主键…；显示列=col；单位注记…）`；关系行
`源.列 →(kind) 目标.主键`（m:n 追加 `经 中间集`）。注入预算不变（50/100），
m:n 中间集占用一条关系预算。

## D6 风险面

- 免费档档位闸：对象端点必须复用关系物化的 PLAN_TIER_REQUIRED 闸——
  不新写档位判断。
- `declared_kind` 与归一化存储的双形状是本 change 最易错点：图谱端点、
  遍历、物化校验三处读方向必须统一走 service 层的「有效方向」函数，
  禁止各处自行翻转（tasks 验收项覆盖三处对拍）。
- 对象端点不进数据面 REST（wdk 轨道）——它们是实体语义面，不是数据面；
  数据面契约零变化。
