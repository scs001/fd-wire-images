# Proposal: wire-team-spaces-v2（团队空间二期：多团队 + 席位配额 + 角色变更）

## Why

ROADMAP 方向 B 的最后一块产品级缺口。v0（wire-team-spaces，2026-10-12）交付了
N:1 成员 + wsi- 邀请码 + 三角角色门禁 + append-only 审计，但三条边界把团队
钉死在小尺度：**一人一队**（`customer_members.logto_sub` 全局唯一——顾问/
外部协作者不能同时进两个客户的队）、**无席位配额**（任何档位都能无限邀人，
套餐的商业边界在团队维度上不存在）、**角色不可变更**（viewer 转正要撤销重邀，
成员行历史断链）。

二期把这三条边界各销一条，让「团队」成为可售卖的形态：

1. **多团队**：成员行唯一键从「sub 全局唯一」改为 `(customer_id, logto_sub)`
   唯一；新增 per-sub **活跃团队指针**（`member_active_team`）+ 切换端点；
   owner 也可受邀入他队做 guest。解析层仍收敛在 `resolve_member_role` 单点。
2. **席位配额**：档位 `limits_json` 增 `seats` 键（后台可配优先；代码缺省
   free=1 / basic=3 / standard=10，旧禁用档=1），**零超额哲学**——超配即停
   （生成邀请与兑换双闸 409），无按席位单价，不发明新商业口径。存量超配成员
   grandfather（只出不进）。
3. **角色变更**：`PATCH /api/console/team/members/{sub}` owner-only，
   editor↔viewer 原地改，team_events 留痕，下次请求即生效。

## What Changes

- **数据层（alembic 0033）**：`customer_members` 唯一约束
  `uq_customer_members_logto_sub` → `uq_customer_members_customer_sub`
  (customer_id, logto_sub)（SQLite 走 batch 重建，PG 直改）；新表
  `member_active_team`（logto_sub PK、customer_id、updated_at）；存量成员行
  回填活跃指针（各自现队）；PG14:5433 + SQLite 双方言验证。
- **解析层（console/auth.py）**：`resolve_member_role` 改为
  **指针优先 → owner 回退 → 首个成员行回退**；指针指向已失效成员行时同事务
  自愈（重指他队/清除）。三个 require_* 依赖零签名变化，全部既有路由继续
  零改动受保护。
- **团队端点（console/team.py）**：新增 `GET /api/console/teams`（我的队清单：
  自有队 + 成员队，含角色与活跃标记）、`POST /api/console/team/switch`、
  `PATCH /api/console/team/members/{sub}`；`GET /api/console/team` 扩展
  seats_used/seats_limit；兑换路径解锁多队（同队重复 409、席位双闸）；
  撤销成员时活跃指针同事务处置。
- **席位配额（billing 联动）**：SEAT_DEFAULTS 代码缺省 + `limits_json.seats`
  覆盖；邀请生成与兑换两处闸门；免费档=1（=仅 owner，团队功能天然付费解锁）。
- **Web（console）**：TeamPage 席位用量展示 + 角色变更控件 + 「我的团队」
  切换面（多队时）；i18n 双语；ConsoleLayout 顶栏活跃队标识（多队身份时）。
- **文档**：DocsView「团队空间」章节扩写二期语义；ROADMAP 方向 B 勾注。

## Impact

- 代码：`api/fd_wire_api/console/{models,auth,team}.py`、`billing/plans.py`
  （缺省映射，不动 seed 策略）、alembic 0033、`web/src/console/TeamPage.tsx`
  等；CustomerMember 消费面已核实仅 console 三文件（无暗依赖）。
- 兼容：存量单队用户零感知（指针回填=现队、owner 解析序不变）；member
  自撤/owner 不可撤等 v0 语义原样保留。
- 风险标注（实施中重点验证）：解析层是全控制台鉴权咽喉——指针自愈路径、
  guesting owner 暂失自家 owner 面（切回即恢复，切换器明示）为刻意语义，
  design.md D2/D5 有裁决。

## Non-Goals

- **邮件邀请**：全舰队无 SMTP/邮件通道基建（仅链接复制邀请继续）——等邮件
  通道立项后另行挂接，不在本批发明邮件服务。
- **按席位超额计费**：零超额哲学（用完即停），无席位单价发明；席位数字属
  产品可调项（后台 limits_json 或代码缺省），建议值见 design.md D3。
- **团队所有权转移**（owner 让位/团队易主）：v0 把 owner 绑在
  billing_customers.logto_sub 身份上，转移=身份级变异（sub2api 映射/cell
  归属连带），成本>当前价值，留 v3 連同「团队实体化」一起裁决。
- **跨队数据可见性**：不存在「同时看两队」——活跃队指针即全部上下文，
  切队=切数据面，与现有 per-customer 隔离红线一致。
