# wire-entity-v2 — Tasks

## 1. 数据模型与迁移

- [x] 1.1 Alembic 迁移：`entities` 增 `display_column`/`column_notes`/
  `declared_kind`/`junction_dataset_id`/`junction_source_column`/
  `junction_target_column`（双方言对拍；存量行新列空值零漂移）
  ——验证 = **alembic 真链 upgrade head 实证（sqlite，0041→0042，2026-10-29）**，
  列存在断言过；PG 侧 rollout 时同跑（全部可空列，风险低）；migrate 测试链头
  断言五处同步 0042
- [x] 1.2 关系 service 扩展：kind 枚举四值、1:n 归一化存储（declared_kind 标记）、
  正反向查重 409（归一化后同形）、m:n 三键校验（中间集属本企业、两列 ∈ 中间集
  列快照）——验证 = 校验矩阵 pytest（m:n 缺中间集 422 / 列不在中间集 422 /
  反向重复 409 / 1:n 源列非主键 422）

## 2. 对象浏览端点

- [x] 2.1 「有效方向」service 函数（`declared_view`：1:n 归一化存储→声明方向
  翻转）+ 图谱/创建响应/遍历三处接同一函数——验证 = 图谱 edge 按声明方向
  （1:n kind 呈现）+ 创建响应按声明方向 + 遍历按存储侧分支 三处对拍 pytest
- [x] 2.2 三端点（列表/详情/遍历）：引擎既有查询工具复用（零新引擎工具）、
  limit 钳制、免费档 403 档位闸（沿关系物化先例）、跨企业 404、零行
  「无此对象」found=false 态、多列主键 422 如实
  ——验证 = 端点矩阵 pytest（引擎调用形状断言：contains 显示列 / eq 主键 /
  遍历 filters）
- [x] 2.3 m:n 遍历两段查询 + m:n 物化 422 如实——验证 = 中间集查询→目标集
  in 两段断言；物化调用 422

## 3. MCP 对象工具（wire-ops-mcp）

- [x] 3.1 `wire_object_search` / `wire_object_get` / `wire_object_relations`
  薄壳直调端点（只读；Bearer 转发；免费档 403/他企 404 由上游如实转述）
  ——验证 = create_server 注册探针 16 工具含三新工具；wire-ops 测试套件 32 全绿
- [x] 3.2 `tool-surface.json` 增列三工具（customer 组；**全只读故不入 tool_risk
  写/费用清单**，符合壹座 risk 契约「只复用既有值」）+ 描述更新 + api 测试
  fixture 同步——验证 = `check_tool_surface.py` 过（rollout 时）、surface 报告
  测试全绿

## 4. 控制台与文档（web）

- [x] 4.1 声明表单扩展（显示列下拉/列注记 JSON 输入/m:n 中间集三字段）——
  验证 = vitest 全量（1024 全绿，含既有 33 实体页测试零回归——请求体断言
  更新为含新键 null）
- [x] 4.2 对象浏览节（EntityObjectBrowser：实例列表 q 检索+分页 → 详情
  属性按列注记渲染 → 关系下拉遍历关联对象；免费档由后端 403 如实呈现）
  ——验证 = tsc 全绿 + 组件随页面挂载（数据驱动渲染）
- [x] 4.3 i18n zh/en 全量 + 文档页「实体」章节 v2（双语：显示列/1:n/m:n/
  对象浏览/工具面口径，含「自有对象搜索在柏讯入口、共享源搜索在 business-mcp」
  的定位语句）——验证 = tsc 全绿 + 文档键双语在位

## 5. 测试与收口

- [x] 5.1 AI 注入对拍：属性元数据（显示列/列注记）+ m:n 中间集进块、空图谱
  零漂移（注入函数空图谱 None 不变）、超限截断沿用——验证 = prompt 构造
  pytest（v2 字段断言）
- [x] 5.2 `openspec validate wire-entity-v2 --strict` 绿；全 api 套件
  1505 全绿（连续两轮）；web 1024 全绿
- [x] 5.3 ROADMAP-v2 Wave C 勾选同步 + 真库端到端冒烟 + 归档。**已上产
  2026-10-30**：迁移 0042 双库落位（entities 两列 + entity_relationships 四列）；
  真库冒烟——建实体 201 且 `display_column`/`column_notes` 原样回显、对象列表
  200、graph 200、1:n 关系 201（服务端归一化为 n:1 存储）、m:n 非主键源列
  如实 422。冒烟实体已清理。
