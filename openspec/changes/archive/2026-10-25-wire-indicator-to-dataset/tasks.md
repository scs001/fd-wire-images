# Tasks: wire-indicator-to-dataset

## 1. API 建集通路

- [x] 1.1 `datasets/schemas.py`：`DatasetSource.kind` Literal 扩 `"indicator"`；新增 indicator 载荷模型（indicator_id/entity_type/entity_id/start/end，范围与格式校验 422 文案逐字冻结）
- [x] 1.2 `datasets/routes.py`：`create_dataset` 分派加 indicator 分支——免费档本地物化 / cell 档引擎三段；同 customer+indicator_id 幂等 409 带既有 key（D4）
- [x] 1.3 新模块 `datasets/indicator_fetch.py`：经 `NativeMcpClient` 调 `indicator_series_read`（INDICATOR_FETCH_URL/TOKEN 配置 + settings 测试）；行形状契约 fixture（date/value/unit/brand）与上游 change 契约对拍
- [x] 1.4 溯源账本 `dataset.create` 事件（subject 携 indicator/entity/range）；`source_kind="indicator"` + `source_ref=indicator_id` 落行
- [x] 1.5 配额闸接对：免费档行数/存储闸（超限整批拒绝零半写）、cell 档存储闸；`metering` 不记此类调用（零计费口径）
- [x] 1.6 负向面：上游不可达 502 如实、超范围 422、registered 指标 422「尚未认证」（导流文案指需求单）、未登录 401

## 2. 控制台/门户面

- [x] 2.1 目录详情页（portal）：verified 指标「落成数据集」入口（实体选择 + 时间范围默认近10年 + 提交）→ 成功跳数据集详情；registered 维持需求单预填不变
- [x] 2.2 数据集列表/详情：`source_kind=indicator` 徽标（双语）+ source_ref 展示 indicator_id
- [x] 2.3 i18n 双语键位（Edit 唯一上下文替换 + 祖先链校验，i18n 坑册纪律）
- [x] 2.4 免费档入口照 free-tier-data 口径渲染（配额内联，非升级墙）

## 3. 测试与验证

- [x] 3.1 api 套件：建集双档 happy path / 幂等 409 / 配额闸 / 上游失败 502 / registered 422；PG 方言腿跑 datasets 路径
- [x] 3.2 web 组件测试：详情页导流表单 / 409 重复提示 / 徽标渲染（2026-10-25 补 409 表单与 indicator 徽标两测，全量 1024 绿）
- [x] 3.3 生产冒烟（真账号）：目录选指标 → 建集 → 行数与上游对账（抽 3 行 date/value 逐一对照）→ derive 派生 → 看板绑数 → 溯源事件可查；免费档与 cell 档各一轮（2026-10-25：wire-tester1/2 双档全过——31/31+26/26+25/25 三集对账零差异；derive 10 行；看板 contract 刷新 snapshot 带真值；409/422/如实空 422 全过；429 行闸=目录无日频 verified 指标线上不可触发，单测已覆盖；冒烟抓出 indicator 注册事件 subject 缺 key 致时间线不可见，当日修复滚版 sha-877cc66 并复验 timeline 可见）
- [x] 3.4 归档前核对：validate --strict 零 error（CLI 中文 spec 误报已知）；tasks 与测试对账（juchuang 教训）（2026-10-25：strict valid；api 1461/web 1024 全绿与勾选对账一致）
