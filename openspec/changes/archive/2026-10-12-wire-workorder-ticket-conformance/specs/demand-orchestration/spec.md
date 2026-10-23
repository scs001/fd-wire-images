## ADDED Requirements

### Requirement: 生成工单舰队 schema 合规

wire 侧生成的 health-loop 工单 SHALL 满足舰队工单 schema（`fd_industry_data.health.ticket.validate_ticket`）的必填字段：`ticket_id`（与落盘文件名主干一致）、`source`、`unit`、`created_at`（出单时刻 UTC ISO）、`evidence`（dict）、`verify`、`lifecycle`（初始为空列表）。`evidence` SHALL 仅含脱敏事实（需求单编码、客户代号、源地址、公开数据声明），SHALL NOT 含 contact_email 或公司名。同日多次出单的 `ticket_id` SHALL 保持一致；`created_at` 按每次出单时刻如实记录。

#### Scenario: 出单通过舰队验票
- **WHEN** 对任一带合法源地址的需求单执行转生成工单
- **THEN** 产出的工单 YAML 含全部必填字段且类型合法（等价于舰队 validate_ticket 零错误），萬星生成回合的 ticket_valid 验证环不再因工单自身缺字段判红

#### Scenario: 脱敏事实进 evidence
- **WHEN** 工单构造完成
- **THEN** evidence 含需求单编码与客户代号，不含 contact_email 与公司名

#### Scenario: ticket_id 与文件名一致
- **WHEN** 工单以文件名 `<YYYYMMDD>-wire-<demand.code>.yaml` 落盘
- **THEN** YAML 内 ticket_id 等于该文件名去 `.yaml`，同日重复出单不漂移
