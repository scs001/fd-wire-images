# Tasks: wire-team-spaces

- [x] 1. api：迁移 0027（customer_members/customer_invites/team_events）+
  模型；链头断言测试随链推进
- [x] 2. api：身份解析升级（owner→成员回退，返回 (customer, role)）+ 方法级
  角色门禁入 require_console_customer + 密钥物面 owner-only
- [x] 3. api：session 邀请兑换分支（入队/码失效/已归属拒/无码零变化）+
  团队管理四端点（owner-only）+ team_events 留痕
- [x] 4. api 测试：邀请全环/门禁矩阵（viewer 变更 403、密钥物 editor 403、
  owner 全通）/撤销即 401/无码首登零回归；全套绿
- [x] 5. web：「团队」页（owner 名单+一次性码+撤销；成员只读+角色徽标）+
  api client + testing fakes + 测试；tsc/现有套件绿
- [x] 6. 发布：快照+GHA api/web 镜像+滚版（maxUnavailable 策略已在）
- [x] 7. 生产冒烟：owner 生成邀请码→码只显一次→作废另一码；viewer 席位
  无真用户=负路径如实（已归属 sub 兑换拒）；GET /team 渲染
- [ ] 8. 归档+ROADMAP 方向 B 标注+记忆
