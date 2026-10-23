## MODIFIED Requirements

### Requirement: 状态机

产物状态为 draft → published → archived 单向迁移，archived → draft 恢复允许；
每次迁移留操作记录；published 是对外稳定引用位——**published 仪表盘可生成签名
分享链接（见 dashboard-share），archived 后链接即失效**；物化执行挂接点在本
能力之外。

#### Scenario: 发布与归档
- **WHEN** 客户依次对 draft 产物执行发布、归档
- **THEN** 状态依次迁移且操作留档；归档后 spec 修改被拒并提示恢复路径

#### Scenario: 非法迁移拒绝
- **WHEN** 对 archived 产物直接执行发布
- **THEN** 返回 409 并说明合法迁移路径

#### Scenario: 归档断分享
- **WHEN** 产物归档后既有分享链接被打开
- **THEN** 链接失效（404），恢复 published 后需重新生成链接
