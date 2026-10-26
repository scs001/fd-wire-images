## ADDED Requirements

### Requirement: 产物 SHALL 记录生产来源指针

产物 SHALL 支持可选的生产来源指针，标识该产物由哪个部署、哪个舰队岗位、哪个回合产出；指针随版本行留痕，append-only。手工创建与控制台 AI 创建的产物 SHALL 保持来源为空或标为人工，其余生命周期语义（draft/published/archived、版本链、草稿层）SHALL NOT 因该字段改变。

#### Scenario: 舰队产物带来源

- **WHEN** 产物经产物构建 MCP 由舰队岗位创建
- **THEN** 其版本行携带来源指针（部署、岗位、回合），且可在产物详情读出

#### Scenario: 手工产物来源为空

- **WHEN** 客户在控制台手工创建或经控制台 AI 草稿转正
- **THEN** 来源指针为空或标为人工，版本链与状态机行为与现状逐字节一致

#### Scenario: 来源不参与鉴权

- **WHEN** 任意来源的产物被读取、更新或分享
- **THEN** 客户域隔离与权限判定只看产物归属，不看来源指针
