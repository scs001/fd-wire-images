# Tasks

## 1. api：聚合端点

- [x] 1.1 `fd_wire_api/workbench.py`：`GET /api/v1/workbench`（require_console_customer_id；platform/datasets/ai 三会话依赖）——datasets 总数+分层+最近≤5+免费配额（复用既有 quota 服务语义，付费 null）、packs 订阅数+最近≤5（名称取静态定义）、artifacts 活跃计数+五类计数+最近≤5、alerts（alert_rule 计数/firing/24h 事件数）、demands（进行中计数+最新一单）
- [x] 1.2 `app.py` 挂载 router
- [x] 1.3 测试 `tests/test_workbench.py`：聚合正确性（各 section 数字与种子数据一致）、最近列表排序与截断、免费配额内嵌、付费 null、跨客户隔离（B 零出现）、未登录 401、无数据空态各段零值
- [x] 1.4 变异自证：注释掉任一 section 的 customer 过滤 → 隔离测试红（证明测试咬合）

## 2. web：工作台页

- [x] 2.1 `console/WorkbenchPage.tsx`：fetch 聚合端点；六段卡片（计数+最近+空态 CTA）；免费配额内联条；壹座紧凑入口卡（demo 页主链+壹座次链，无账号明文）
- [x] 2.2 ConsoleLayout 增 `workbench` 页签（首位）+ ConsoleView 路由渲染；landing_tab 值域含 workbench（默认落地不变）
- [x] 2.3 i18n zh/en 全量键
- [x] 2.4 测试：渲染各段计数与最近条目、空态 CTA、深链 href、壹座卡无账号明文、页签存在且首位、landing=workbench 程序跳转不回写（沿用既有 suppress 测试范式）

## 3. 文案修正（零行为变更）

- [x] 3.1 ROADMAP §3「已知的可见性三断点」段改为已闭环口径；§6 近期清单标注（三断点/产物转正/PG 起步均上产，个人侧 MVP 剩薄工作台→本轮）
- [x] 3.2 `deploy/k8s/fd-wire-api.yaml` datasets 库注释改 PG 已切口径（10-11 凌晨 82ccf0c）

## 4. 门禁与发布

- [x] 4.1 api pytest 全绿 + web vitest 全绿 + tsc 0 + `openspec validate wire-personal-workbench --strict`
- [x] 4.2 发布：双推 → 快照仓 → CI 绿 → relay → 双清单滚版 → ArgoCD
- [x] 4.3 生产冒烟：真账号工作台渲染（各段计数对真库可验）+ 免费档配额条 + 裸进落地不变回归

## 5. 归档

- [ ] 5.1 `openspec archive wire-personal-workbench -y` + `validate --specs` + ROADMAP 方向 D 标注 + 记忆更新 + 快照仓同步
