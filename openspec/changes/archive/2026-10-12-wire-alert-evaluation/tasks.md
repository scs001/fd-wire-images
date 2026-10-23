# Tasks: wire-alert-evaluation

- [x] 1. api：规则契约校验（alert_spec 纯函数：形状/区间/枚举/URL/数值，
  错误定位字段）+ alert_states/alert_events 模型与迁移 0028（链头推进+
  链头断言测试随链）
- [x] 2. api：评估引擎（tick 循环照 schedules 范式：due 扫描/published 门/
  查询复用 parse_panel_query+_query_dataset/归约/状态机边缘转换/冷却再通知/
  webhook 5s 超时/notify_failed/error 事件）挂 app lifespan（env 可关）
- [x] 3. api：管理端点（GET /api/v1/alerts/events 客户隔离倒序；
  POST /api/v1/artifacts/{id}/alert/test 试运行零副作用；非 alert_rule 404）
- [x] 4. api 测试：契约矩阵/状态机往返（firing→resolved 事件序列、持续真
  不重复）/冷却再发/webhook 失败如实/存量不合形不崩溃/试运行零副作用/
  客户隔离；全套绿
- [x] 5. web：ArtifactsPage alert_rule 行状态徽标+最近评估时间；详情区事件
  列表+试运行按钮（value/wouldFire 展示）+ api client + i18n + 测试；tsc/套件绿
- [x] 6. 发布：**先落迁移 0028（离线 SQL 双库）再滚版**（避免 create_all 先
  建表撞迁移——23 轮课）；快照+GHA+滚版
- [x] 7. 生产冒烟：API 建规则（免费档数据集 sum>阈值）→发布→等首评估→
  徽标 firing+事件落库→调阈值→resolved→test 端点零副作用；webhook 指向
  不可达地址验 notify_failed 如实
- [ ] 8. 归档+ROADMAP（方向 D 我的规则落地）+记忆
