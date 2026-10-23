# Design: wire-team-spaces-v2

## D1 活跃指针为什么独立成表（而非塞进 member_preferences）

`member_preferences` 是 per-sub 的**个人 UI 偏好域**（语言/落地页/置顶看板，
wire-personal-preferences 语义），活跃团队是**鉴权解析域**（决定每个请求操作
哪个客户）。两域生命周期与读方不同（偏好只在个人面读；指针每个控制台请求都
经 resolve_member_role 读）。塞列会造出「改偏好表的接口能影响鉴权上下文」的
隐性耦合。故新表 `member_active_team`（ConsoleBase 同库，logto_sub 主键）。

## D2 解析序与自愈（本 change 的咽喉决策）

`resolve_member_role(sub)` 新序：

1. **指针优先**：`member_active_team` 命中 → 校验该 (customer, sub) 成员行
   仍在（或该 customer 就是 sub 自有队——owner 自切场景）→ 命中返回
   (customer, 成员行 role | 'owner')；
2. **owner 回退**：无指针 → `billing_customers.logto_sub` 命中 → (自有客户,
   'owner')（存量 owner 零感知）；
3. **首队回退**：无指针且有成员行 → id 最小的成员行（首队）→ (该队, role)；
4. 均未命中 → None（401 面文案不变）。

**自愈**：指针指向的成员行已不存在且该客户也非自有队（被撤销）→ 同事务清除
指针并按 2/3 重解析。撤销成员端点在删成员行的同事务里主动处置指针（有他队
成员行→重指 id 最小者；否则删除指针行），自愈只是兜底（并发窗口）。

裁决理由：指针优先而非 owner 优先——owner 优先会让「受邀 guest 的 owner」
永远解析回自家队，受邀失去意义；指针显式表达「我现在在哪队工作」，切换端点
是唯一入口，语义可预期。

## D3 席位口径与数字

- **口径**：seats_used = 该客户成员行数 + 1（owner 占一席）。editor/viewer
  都占席（都消费团队资源与配额）。邀请上限 = seats_limit - seats_used。
- **数字**：`limits_json.seats` 后台可配优先；缺省映射
  `SEAT_DEFAULTS = {"free": 1, "basic": 3, "standard": 10}`，未知/旧禁用档=1。
  免费档=1 即「团队功能付费解锁」（free 只有 owner，邀请 409 引导升级）。
  数字是产品建议值：改 `limits_json` 即时生效（不走 seed——seed 策略=已存在
  行不动），无需发版。
- **双闸**：生成邀请时校验（超配 409，不产码）；兑换时再校验（防降档/并发
  竞态），超配**拒且码保活**（不销毁邀请，腾出席位即可继续用）。
- **grandfather**：存量超配成员行不追溯（移除是唯一出口）；团队面如实展示
  seats_used/seats_limit（超配时显示警示态）。

## D4 兑换语义（多队解锁后的规则）

`redeem_invite`：同 (customer, sub) 已有成员行 → 409「已是该团队成员」；
席位超配 → 409（码保活）；否则建成员行 + 码置 used + team_events。
**首队才落指针**：无指针时新队成为活跃队；已有指针（在别队工作）时只入队
不切上下文——接受邀请不应劫持当前工作上下文。owner 兑换他队邀请从「已有
归属拒」解锁为正常入队。

## D5 guesting owner 的密钥物面（刻意语义，非缺陷）

owner 把活跃指针切到 guest 队后，其角色=该队成员角色（editor/viewer）：
- guest 队的密钥物端点 403（角色门禁天然覆盖，v0 规则零改动）；
- **自家队的 owner 面暂时 403**（解析到 guest 队）——切换器明示当前所在队，
  切回即恢复。不做「密钥物面永远解析自有队」的特例：那会造成一个请求里
  两个客户上下文（数据面=A、密钥面=B），split-brain 比暂时失权更危险。

## D6 计量与审计归属

一切计量/配额/数据面继续按**活跃客户**归属（现状机制零改动：所有端点拿到的
customer 都来自解析层）。切队=切计量上下文；team_events 只记团队变更
（成员/邀请/角色），**切换是个人 UI 态不入 team_events**（per-sub 指针表自身
有 updated_at，审计问「谁在何时操作了哪个客户」由各端点既有留痕回答）。

## D7 迁移（0033）双方言要点

- PG：`DROP CONSTRAINT uq_customer_members_logto_sub` → `ADD CONSTRAINT
  uq_customer_members_customer_sub UNIQUE (customer_id, logto_sub)`；
  建新表 + 回填（每成员行 INSERT 指针行 ON CONFLICT DO NOTHING）。
- SQLite：无 DROP CONSTRAINT——`batch_alter_table` 重建 customer_members
  （成员表行数小，重建可承受；照抄 0027 的建表定义换约束）。
- 布尔默认值陷阱不涉（本迁移无布尔列）；回填后必须验证：存量单队成员解析
  结果与迁移前逐 sub 一致。

## D8 测试策略

- 解析层单测：指针/owner/首队三序、自愈、guesting owner 403 面；
- 兑换：多队解锁、同队 409、席位双闸（生成/兑换）、码保活、首队指针；
- 席位：grandfather、limits_json 覆盖、免费档引导文案；
- 角色变更：owner-only、owner 行 409、即时生效、team_events；
- 切换：非成员目标 404/403、切换后数据面/角色联动、teams 清单正确性；
- web：TeamPage 席位/角色/切换三件 + i18n；
- 迁移：SQLite 全链 + PG14:5433 scratch-db 升级+回填对账。
