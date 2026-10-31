# wire-entity-v2

## Why

实体层 v0 是「元数据注释」：实体只有业务名+数据集+主键，关系只有 n:1/1:1，
没有属性元数据、没有对象实例面、集群无法经工具面遍历客户自有对象——本体对
集群不可消费（ROADMAP-v2 Wave C）。本 change 把实体层升为「可浏览、可遍历、
可被集群消费的对象世界」：这是柏讯承接 Foundry Ontology 器官的主件。

## What Changes

- **属性元数据**：实体可选声明显示列与列注记（单位/格式，纯描述元数据），
  对象列表/详情/AI 上下文使用；存量 v0 声明零迁移。
- **关系扩展**：kind 补 `1:n`（n:1 的反向声明，不新增存储）与 `m:n`
  （经中间数据集声明，两列各指向两端实体主键）；m:n v0 可声明可遍历、
  物化端点不支持（如实 422）。
- **对象浏览（owner 输出面）**：实例列表（搜索/筛选/分页）→ 对象详情
  （属性+沿关系遍历的关联对象）；只执行声明查询（沿 record-view 读边界纪律）；
  cell 档专属，免费档保持现状（声明+图谱）。
- **MCP 对象工具**：wire-ops-mcp 新增 `wire_object_search` / `wire_object_get` /
  `wire_object_relations`——**集群消费客户自有对象的主通道**；这是「搜索放
  business-mcp」定位修正的落点：共享源搜索在 business-mcp，自有数据对象搜索
  在 wire 入口工具面，控制台不做全局搜索。
- **AI 上下文注入 v2**：注入块含属性元数据与 m:n 关系形状；有界注入与空图谱
  零漂移纪律不变。
- 控制台实体页扩展（声明表单 + 对象浏览节）；文档「实体」章节 v2。

## Capabilities

### New Capabilities

（无——实体域能力仍归 `entity-layer` 一个 capability）

### Modified Capabilities

- `entity-layer`: ①关系声明与纪律扩 `1:n`/`m:n`；②新增实体属性元数据；
  ③新增对象实例浏览端点；④新增 MCP 对象工具；⑤AI 构建上下文注入 v2；
  ⑥控制台实体页扩展；⑦文档与红线 v2。

## Impact

- **api**：`entities/` 模块扩展（属性元数据列、关系 kind 枚举、对象浏览端点）；
  对象查询复用引擎既有查询工具（零新引擎工具）。
- **wire-ops-mcp**：工具面 +3（`wire_object_*`），`tool-surface.json` 同批更新。
- **web**：EntitiesPage 扩展 + i18n + 文档页。
- **不改**：实体/关系既有 CRUD 语义、物化端点（n:1/1:1 语义不变）、计量轨道、
  公开面红线；引擎零改动。
