# Tasks — wire-dashboard-refresh

## 1. api

- [x] 1.1 无头刷新轮封装：复用 ai/loop 既有服务（先侦察 loop.py/service.py 的会话模型；抽最小可复用段或以一次性会话承载系统指令），输入=当前 spec JSON+刷新指令，输出=更新后 spec；LLM 输出解析失败→502 带原因不落版本
- [x] 1.2 POST /api/v1/artifacts/{id}/refresh：登录+跨客户 404+kind 校验；单产物进程内锁（进行中 409）；成功=新版本落盘（同 PUT 语义与审计）；无 cell→档位提示 403 语义；pytest 全路径
- [x] 1.3 ai/tools.py dashboard_spec_create 描述补 source 标注提示（刷新依赖）；相关快照测试更新

## 2. web

- [x] 2.1 DashboardPane 查看态「刷新数据」按钮：调用/成功提示 v{n} 并重渲染/失败如实/进行中 loading；archived 态无按钮；i18n ZH/EN
- [x] 2.2 组件测试：刷新成功流（mock 新版本返回）/失败态/409 提示

## 3. 验证

- [ ] 3.1 api 全量绿+web 全量绿+tsc 干净
- [ ] 3.2 生产冒烟（随发布）：未登录 401；登录态对无 cell 账号的档位提示（日志或响应验证）
