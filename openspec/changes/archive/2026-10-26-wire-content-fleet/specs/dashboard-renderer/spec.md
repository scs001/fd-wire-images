## ADDED Requirements

### Requirement: dashboard spec SHALL 可经 MCP 产出且与渲染同路径

dashboard v1 spec SHALL 可由 AI agent 经产物构建 MCP 产出；其结构校验 SHALL 走与 spec 更新相同的 `validate_kind_spec` 分派，渲染 SHALL 走与既有看板相同的渲染路径（宽容校验、坏面板隔离、快照呈现）。SHALL NOT 为 MCP 产出物新增第二套 spec 结构或渲染分支。

#### Scenario: MCP 产出的看板正常渲染

- **WHEN** agent 经 MCP 创建一个含 kpi/line/bar/table 面板的 dashboard
- **THEN** 控制台打开该产物时按既有渲染路径呈现，坏面板隔离规则同样生效

#### Scenario: 结构非法同规拒绝

- **WHEN** agent 提交的 dashboard spec 缺 panels 或字段类型错
- **THEN** 422 拒绝且零半写，与控制台路径一致

#### Scenario: 编辑与版本链不受来源影响

- **WHEN** 客户对一个 agent 产出的看板进行编辑
- **THEN** 保存即新版本、契约保留与类型互转等既有行为逐字节一致
