# Tasks: wire-team-spaces-v2

## 1. 数据层（迁移与模型）

- [x] 1.1 `console/models.py`：`CustomerMember` 唯一约束改
  `(customer_id, logto_sub)`（docstring 同步多队语义）；新增
  `MemberActiveTeam`（logto_sub PK、customer_id、updated_at，ConsoleBase）
- [x] 1.2 alembic `0033_team_spaces_v2`：PG 直改约束（drop
  `uq_customer_members_logto_sub` → add `uq_customer_members_customer_sub`）；
  SQLite `batch_alter_table` 重建（照抄 0027 建表定义换约束）；建
  `member_active_team`；存量成员行回填指针（每行一条，幂等）
- [x] 1.3 迁移双方言验证：本地 PG14:5433 scratch-db（create→upgrade→
  存量行回填对账→drop）+ SQLite upgrade 全链；回填后逐 sub 解析结果与
  迁移前一致（单队存量零感知证明）

## 2. 解析层（console/auth.py）

- [x] 2.1 `resolve_member_role` 改指针优先 → owner 回退 → 首个成员行回退；
  指针悬空同事务自愈（清除+重解析）；docstring 更新解析序与自愈语义
- [x] 2.2 三个 require_* 依赖（customer/member_identity/owner）签名零变化
  复核：全部既有路由零改动；guesting owner 在 guest 队按成员角色门禁
  （密钥物面 403 路径既有规则覆盖，补测试证明）

## 3. 团队端点（console/team.py）

- [x] 3.1 `GET /api/console/teams`：我的队清单（自有队 + 成员队：客户 id/
  名称、角色、活跃标记）+ 当前活跃客户 id
- [x] 3.2 `POST /api/console/team/switch`：body customer_id；校验目标为
  自有队或成员行所在队（否则 404/403 如实）→ 更新指针；非 owner 也可切
  自己的指针（个人面语义）
- [x] 3.3 `PATCH /api/console/team/members/{sub}` 角色变更：owner-only、
  editor|viewer 校验、同值幂等、非成员 404、owner 行 409、team_events
  'member.role_changed'（old→new）；下次请求即新角色
- [x] 3.4 撤销成员端点补指针处置：同事务删成员行时，其指针有他队成员行→
  重指 id 最小者；否则删指针行
- [x] 3.5 `redeem_invite` 多队解锁：同 (customer, sub) 409、席位兑换闸
  （超配 409 且码保活）、首队落指针/后续入队不动指针、owner 兑换他队正常
  入队；team_events 留痕不变
- [x] 3.6 `GET /api/console/team` 扩展 seats_used/seats_limit（grandfather
  超配如实展示）

## 4. 席位配额（billing 联动）

- [x] 4.1 `billing/plans.py`：`SEAT_DEFAULTS` 缺省映射（free=1/basic=3/
  standard=10，未知/禁用=1）+ `seat_limit(session, plan_code)` 读取函数
  （limits_json.seats 覆盖缺省；不动 seed 策略）
- [x] 4.2 邀请生成闸：`POST /api/console/team/invites` 前置校验
  seats_used ≥ seats_limit → 409（含档位/席位数字/升级指引文案）

## 5. API 测试

- [x] 5.1 解析层：三序全分支、指针自愈、guesting owner 密钥面 403/切回
  恢复、成员被撤后回退他队/401
- [x] 5.2 兑换与席位：多队解锁、同队 409、生成/兑换双闸、码保活、
  limits_json 覆盖生效、免费档引导、grandfather 展示
- [x] 5.3 切换与角色：teams 清单正确性、switch 404/403、切换后数据面
  联动（如 datasets 列表随活跃队变化）、角色变更四场景（含 owner 行 409
  与 team_events 断言）

## 6. Web 控制台

- [x] 6.1 TeamPage：席位用量条（seats_used/seats_limit，超配警示态）+
  邀请按钮超配时禁用并提示升级；成员行角色变更控件（owner 可见）
- [x] 6.2 「我的团队」面：多队身份时 TeamPage 顶部展示队清单与活跃标记 +
  切换动作（调 teams/switch，切换后刷新全部控制台数据）；单队身份零打扰
- [x] 6.3 i18n zh/en 全量键；vitest 用例（席位/角色/切换三件 + 免费档
  引导态）
- [x] 6.4 ConsoleLayout 顶栏多队身份活跃队标识（轻量徽标，单队不渲染）

## 7. 文档与收尾

- [x] 7.1 DocsView「团队空间」章节扩写：多队/席位/角色变更语义与边界
  （guesting owner 暂失 owner 面、切队=切数据面）
- [x] 7.2 `docs/ROADMAP.md` 方向 B：团队空间二期勾注（多团队+席位配额+
  角色变更；邮件邀请因邮件通道缺位继续挂账）
- [x] 7.3 全量回归：api 测试套 + web vitest + tsc；conformance 自检
  （无越线直读/无客户数据入公开面）

## 8. 部署（生产验证清单，走 GHA→TCR→ArgoCD）

- [x] 8.1 生产迁移与滚版（2026-10-13 完成）
  - 发布通道：**GHA 被账单阻断**（runner 起不来，近四轮全败）→ 绕行 =
    cheap-2（x86_64）本机 docker build 直推 ccr；web Dockerfile 首行
    `# syntax=docker/dockerfile:1` 需 docker.io frontend（境内不通）→
    构建机临时删行 + 轩辕基础镜像
  - 镜像：api + web = `sha-f4970ed`（ccr 远端核验 amd64）
  - 迁移：生产 `wire_platform` + `wire_datasets` 两库 0029→**0033**
    （0030 资产目录/0031 质量/0032 管线三批欠账一并补齐）；约束替换核验
    （uq_customer_members_customer_sub 就位、旧全局唯一消失）、9 张新表齐、
    回填 0↔0（生产成员表空）；滚版前手动全量备份 20261012-215929
  - 滚版：清单 sha → ArgoCD 同步 8455227 → api/web pod 双双 sha-f4970ed
    1/1 Running；pod 内 /health ok
  - 冒烟（生产真实数据）：新端点 `/api/console/teams`、`/api/console/team`
    401 鉴权在（旧镜像下会 404）；`resolve_member_role` 指针优先解析
    →(客户1,'editor')、owner 回退→(客户1,'owner')、未知 sub→None；
    `seat_limit(free)=1`、超配 seats_used=2 如实、席位闸 409 文案含档位
    与升级指引；测试数据已清理（席位回落 1）
  - 工程侧（1-7）前置全绿：api 1193 过 / web tsc 零错 + 46 文件 674 用例、
    双方言迁移（SQLite 链 + PG14:5433 scratch）含回填对账与 downgrade 实证
