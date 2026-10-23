# transform-pipeline 变更（wire-pipeline-actions）

## ADDED Requirements

### Requirement: 控制台重派生与定时管理

派生数据集详情页（derived + cell 档）提供「重派生」按钮：级联默认开可关，
调用既有重派生端点（console 会话凭据），级联各级结果（数据集名/深度/
成功失败与原因）如实呈现，刷新后行数与更新时间同步；并提供「定时重派生」
登记管理：cron 五字段登记/更新、当前登记态（cron/enabled/最近运行时间/
状态/错误/行数）如实呈现、删除登记，naive UTC 口径诚实注记。api 层
（免费档）数据集不出现该入口；管线视图保持只读。

#### Scenario: 手动重派生即级联
- **WHEN** 客户对派生数据集点击重派生（级联开）
- **THEN** 本体与派生子链依次刷新，级联各级结果如实呈现，行数/更新时间同步

#### Scenario: 定时登记管理
- **WHEN** 客户登记 cron 后查看
- **THEN** 登记态与最近运行结果如实呈现；删除后回到未登记态

#### Scenario: 免费档无入口
- **WHEN** 数据集是 api 层（免费档）
- **THEN** 详情页不出现重派生与定时管理入口
