# team-spaces Specification

## Purpose

团队的成员制、角色门禁、席位配额与多团队归属：客户所有者（owner）经一次性
邀请码拉人入队，成员按 editor/viewer 角色行权；同一账号可属多个团队并以
「活跃团队」指针决定请求上下文（切换团队即切换数据面）。席位按套餐档位
设硬上限（零超额：满员即拒，无按席位单价）；全部成员/邀请/角色变更
append-only 留痕（team_events）。owner 身份不可经团队端点变更，邮件邀请
不在本能力面（链接复制邀请在役）。

## Requirements

### Requirement: 成员制与邀请

客户MUST支持多成员：`customer_members` 记录 (customer, logto_sub, role)，
**同一 logto_sub 可属于多个客户**（唯一键为 (customer_id, logto_sub)；
一个 sub 至多每队一行，自有队 owner 与他队成员身份可并存）。owner MUST可
生成一次性邀请码（`wsi-` 前缀，24 小时过期，绑定邀请角色 editor|viewer）；
新 sub 或已有归属的 sub 在 `POST /api/console/session` 携有效码MUST加入
对应客户为成员（不新建客户/不建支付账户/共享该队套餐与配额），码即失效；
对已是该队成员的 subMUST 409 如实拒；无码首登行为MUST与现状一致。被撤销
或离开的成员其会话MUST在下次请求即失效（401 或回退到仍有归属的队）。

#### Scenario: 邀请兑换
- **WHEN** 未映射新 sub 持有效邀请码调 session
- **THEN** 该 sub 成为对应客户成员（携带邀请角色），响应 ready，码失效，
  且该队成为其活跃团队（若此前无活跃指针）

#### Scenario: 码失效如实拒
- **WHEN** 码已被用/过期/不存在，或 sub 已是该队成员，或席位超配
- **THEN** 兑换被拒并给明确原因；席位超配时码保活（不销毁），无码路径
  行为与现状一致

#### Scenario: 多队入队不劫持上下文
- **WHEN** 已有归属（owner 或他队成员）的 sub 兑换新队邀请成功
- **THEN** 其成员行新增、活跃团队指针不变（仍指向原队），切换需显式调用
  切换端点

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

### Requirement: 活跃团队与切换

每个 logto_sub SHALL 至多持有一个**活跃团队指针**
（`member_active_team`：logto_sub 主键 → customer_id）。控制台身份解析
SHALL 按以下顺序收敛于 `resolve_member_role` 单点：有效指针优先（校验
成员行仍在或该客户为其自有队）→ 无指针时 owner（自有客户）→ 首个成员行
（id 最小者）→ 均无则 401。指针指向已失效成员行时系统MUST同事务自愈
（清除并按后续序重解析）。`GET /api/console/teams` SHALL 返回该 sub 的
全部归属（自有队 + 成员队，各含客户名与角色）及当前活跃团队标识；
`POST /api/console/team/switch` SHALL 校验目标为自有队或成员队后更新指针
（否则 403/404 如实），切换仅影响发起者本人。

#### Scenario: 切换生效
- **WHEN** 多队身份的 sub 调 switch 指向其成员队并随后调任意控制台端点
- **THEN** 解析到目标队（角色=该队成员角色），数据面与配额按目标队归属

#### Scenario: guesting owner 的 owner 面
- **WHEN** 某队 owner 将活跃指针切到其 guest 队后访问自家队 owner-only 端点
- **THEN** 403（角色不足）；切回自有队后恢复 owner 权限

#### Scenario: 指针自愈
- **WHEN** 成员被撤销（撤销端点或并发窗口）导致指针悬空
- **THEN** 其下次请求按 owner/首队序重解析（有他队即落他队，均无则 401），
  不产生半归属态

### Requirement: 席位配额

套餐档位 SHALL 携带席位上限：`limits_json.seats` 后台配置优先，缺省映射
free=1、basic=3、standard=10（未知/禁用档=1）。席位口径=成员行数+1
（owner 占席；editor/viewer 均占席）。邀请生成与兑换两处MUST校验席位：
生成侧超配MUST 409 并给档位、席位数字与升级指引（不产码）；兑换侧
（`POST /api/console/session` 携码路径）超配MUST拒绝并给明确原因（沿用
该端点既有 failed 响应语义）且MUST保活邀请码。席位上限为硬停（零超额
哲学，无按席位单价）。存量超配成员MUST grandfather（不追溯、移除是唯一
出口）；团队面MUST如实展示 seats_used/seats_limit（超配呈警示态）。

#### Scenario: 生成闸
- **WHEN** owner 在席位数已满（seats_used ≥ seats_limit）时生成邀请
- **THEN** 409（含席位数字与档位），不产生邀请码

#### Scenario: 兑换闸竞态
- **WHEN** 邀请生成后档位被调低或并发邀请导致兑换时席位已满
- **THEN** 兑换被拒（failed 语义+明确原因）且邀请码保活；腾出席位后
  同码可再兑换

#### Scenario: 免费档引导
- **WHEN** free 档（seats_limit=1）owner 尝试生成邀请
- **THEN** 409 并引导升级；升级后无需迁移即可邀人

### Requirement: 成员角色变更

`PATCH /api/console/team/members/{sub}` SHALL owner-only：将成员角色在
editor|viewer 间变更（同值幂等成功）。目标非成员行MUST 404；owner 自身
（隐含 owner 行）MUST 409 并说明 owner 角色不可经本端点变更。变更MUST
append-only 留痕（team_events，含旧→新角色），且MUST即时生效（目标成员
下次请求即按新角色门禁）。

#### Scenario: 角色变更生效
- **WHEN** owner 将 viewer 成员改为 editor，该成员随后调变更类端点
- **THEN** 变更成功（此前 403 的调用放行），team_events 有 old→new 留痕

#### Scenario: owner 行不可改
- **WHEN** owner 对自己的隐含 owner 行调角色变更
- **THEN** 409（owner 角色非本端点面），team_events 无变更留痕
