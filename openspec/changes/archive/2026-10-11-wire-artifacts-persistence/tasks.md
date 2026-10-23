# Tasks — wire-artifacts-persistence

## 1. 数据模型与迁移（api）

- [x] 1.1 alembic 迁移：`artifacts`（id、customer_id、kind、title、status、version 计数、origin_conversation_id 可空、created/updated）与 `artifact_versions`（artifact_id、version、spec JSON、operator、created）append-only；迁移幂等，drift check 通过
- [x] 1.2 模型约束：kind 限五类枚举（dashboard/composite_indicator/research_report/alert_rule/refresh_schedule）；status 枚举（draft/published/archived）；单测覆盖约束

## 2. API（api）

- [x] 2.1 列表与详情：`GET /api/v1/artifacts`（kind/status 过滤、按 updated 倒序）与 `GET /{id}`；跨客户访问 404（隔离断言）
- [x] 2.2 spec 更新：`PUT /{id}` 校验结构合法（沿用现有最小校验）→ 追加新版本并递增计数；archived 状态拒改（错误明示先恢复）
- [x] 2.3 版本历史：`GET /{id}/versions` 与 `GET /{id}/versions/{n}`；append-only 断言（历史版本不可改）
- [x] 2.4 状态迁移：`POST /{id}/publish`、`POST /{id}/archive`；draft→published→archived 单向，archived→draft 恢复允许（留操作记录）；非法迁移 409
- [x] 2.5 会话草稿导入：`POST /api/v1/artifacts/from-draft`（conversation_id + draft 标识）创建 draft 产物并保留来源指针；草稿不存在/非本人 404

## 3. web 产物页接真（web）

- [x] 3.1 列表切换新端点：从会话聚合改为 `GET /api/v1/artifacts`（来源会话列保留显示 origin 指针）；kind 徽标沿用
- [x] 3.2 spec 编辑保存走服务端（PUT）：保存成功提示新版本号；localStorage 退位为未保存离开提示的本地缓冲；archived 拒改的 UI 呈现
- [x] 3.3 版本历史抽屉：列出历史版本、查看任一版本 spec、可复制；
- [x] 3.4 发布/归档操作与状态徽标；i18n ZH/EN
- [x] 3.5 组件测试：列表/编辑保存/版本抽屉/状态迁移四态渲染断言

## 4. 验证

- [x] 4.1 E2E（测试态）：会话草稿→导入 draft 产物→编辑保存两个版本→发布→归档→恢复→版本历史完整；全程跨客户隔离
- [x] 4.2 回归：既有会话草稿端点与 AI 构建链路测试全绿；产物页既有快照更新后绿
