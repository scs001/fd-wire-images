# Proposal: wire-team-spaces — 团队空间 v0（N:1 成员制+邀请码+角色门禁）

## Why

方向 B 剩余主件「团队空间」。现行控制台严格 1:1（`billing_customers.logto_sub`
唯一映射，一人一客户），企业客户多人协作只能共享账号——无归属、无审计、无
权限差异。v0 把 1:1 升为 N:1：owner 邀请成员（邀请码人工传递，SMTP 未配是
已知边界）、成员分 owner/editor/viewer 三角、viewer 只读。

## What Changes

1. **成员与邀请模型**（迁移 0027）：`customer_members`（customer_id×logto_sub
   唯一，sub 全局唯一=一人至多入一队且不得与任何 owner sub 冲突；role in
   owner|editor|viewer）+ `customer_invites`（一次性码 `wsi-` 前缀、24h 过期、
   绑定角色）+ `team_events` 迁移审计（append-only）。
2. **身份解析升级**：sub → 先 owner 映射、后成员映射；**方法级角色门禁落在
   `require_console_customer` 内部**（viewer 对 POST/PUT/PATCH/DELETE 一律 403，
   现有全部路由零改动即受保护）；**密钥物面（wgk-/服务凭据）owner-only**
   （viewer/editor 不可见明文）。
3. **邀请兑换进开户流**：`POST /api/console/session` 增可选 `invite_code`——
   新 sub 持有效码则**加入既有客户**（建 membership，不建新客户不建 sub2api
   账户、共享套餐与配额）；无码行为不变。
4. **团队管理 API（owner-only）**：GET /api/console/team（成员+待用邀请）、
   POST /team/invites、DELETE /team/invites/{id}、DELETE /team/members/{sub}
   （owner 不可被撤）；事件全量入 team_events。
5. **控制台「团队」页**：owner 可名单/生成一次性邀请码（码只显示一次）/撤销
   成员；成员只读名单+自己的角色徽标。

### v0 边界（显式不做）

- per-seat 计费（成员共享客户套餐与配额）；
- 邮件/短信邀请发送（邀请链接+码人工传递；SMTP 未配为既有挂账）；
- 成员归属多团队、组织间切换、跨客户共享；
- 成员单独的 cell/wgk 凭据签发（共享客户凭据面不变）。

## Impact

api：auth/onboarding/新 team 路由+迁移 0027（双 PG）；web：团队页+api client；
生产 api+web 滚版；引擎/cell 零改动。
