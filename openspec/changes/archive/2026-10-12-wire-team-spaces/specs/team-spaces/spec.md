# team-spaces Specification（delta）

## ADDED Requirements

### Requirement: 成员制与邀请

客户MUST支持多成员：`customer_members` 记录 (customer, logto_sub, role)，
sub 全局唯一（一人至多一队，且不得与任何 owner sub 冲突）。owner MUST可
生成一次性邀请码（`wsi-` 前缀，24 小时过期，绑定邀请角色 editor|viewer）；
新 sub 在 `POST /api/console/session` 携有效码MUST加入既有客户为成员
（不新建客户/不建支付账户/共享套餐与配额），码即失效；无码首登行为MUST
与现状一致。被撤销或离开的成员其会话MUST在下次请求即失效（401）。

#### Scenario: 邀请兑换
- **WHEN** 未映射新 sub 持有效邀请码调 session
- **THEN** 该 sub 成为对应客户成员（携带邀请角色），响应 ready，码失效

#### Scenario: 码失效如实拒
- **WHEN** 码已被用/过期/不存在，或 sub 已有归属
- **THEN** 兑换被拒并给明确原因；无码路径行为与现状一致

### Requirement: 角色门禁

角色 owner|editor|viewer。viewer MUST被拒绝一切变更类调用（POST/PUT/PATCH/
DELETE 一律 403），读面与成员一致；editor 拥有除团队管理与密钥物外的全部
操作；owner 全权。**密钥物端点（客户 wgk- 凭据的明文/铸造/吊销面）MUST
owner-only**（editor/viewer 403）。门禁MUST实现在统一鉴权依赖内（全部既有
路由零改动受保护），且角色判定不得依赖客户端传入。

#### Scenario: viewer 只读
- **WHEN** viewer 调任意变更类控制台端点
- **THEN** 403（角色不足），GET 正常

#### Scenario: 密钥物 owner-only
- **WHEN** editor 或 viewer 访问密钥明文/铸造/吊销端点
- **THEN** 403；owner 正常

### Requirement: 团队管理与审计

团队管理端点MUST owner-only：成员名单+待用邀请列表、生成邀请、作废邀请、
撤销成员（owner 不可被撤）。每次成员/邀请变更MUST append-only 留痕
（谁、何时、何事、对象），审计不可改写。

#### Scenario: 管理 owner-only
- **WHEN** editor/viewer 调团队管理端点
- **THEN** 403；owner 操作成功且 team_events 有留痕
