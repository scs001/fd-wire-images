# Tasks

## 1. api

- [x] 1.1 admin transition 补 `maybe_auto_deliver` 钩子（注入 scope_client_dep，
      与内部端点逐字同语义；scope 失败只落 pending 不打断）
- [x] 1.2 测试：管理台交付自动开 scope（成功 ok/失败 pending 两态）+ 与内部
      端点行为一致性（pytest 全绿）

## 2. web admin

- [x] 2.1 `AdminDemand` 类型补齐 pipeline_events/delivery_events/gate/
      scope_status/scope_name（API 已返回）
- [x] 2.2 派发台每单进度面：事件时间线（七类阶段+时间+ticket_path+PR 链接）
      +gate 摘要+交付状态；未出单「尚未出单」占位；i18n 键
- [x] 2.3 测试：在途单各阶段渲染/未出单占位/交付状态两态（vitest 全绿）

## 3. web console

- [x] 3.1 `CustomerDemand` 类型补 `delivery`；delivered 单渲染交付就绪
      区块（已就绪/准备中/公开授权）；非 delivered 不渲染；i18n 键
- [x] 3.2 测试：交付就绪两态渲染/非 delivered 零渲染/无内部细节字段
      （vitest 全绿+tsc 0）

## 4. 门禁与发布

- [x] 4.1 本地门禁：api pytest 全量+web vitest 全量+tsc --noEmit 0
- [x] 4.2 构建发布→relay→滚版（api+web 双滚）
- [x] 4.3 生产冒烟：管理台交付实弹（scope 触发或如实 pending）+ 双面板渲染
- [x] 4.4 归档+ROADMAP 方向 C 标注+记忆更新
