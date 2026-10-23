# Tasks: wire-entity-link

## 1. 引擎（fd-daas-mcp internal-main，worktree /tmp/daas-entlink）

- [x] 1.1 `customer_dataset_link` 工具：LEFT JOIN/撞名 right_ 前缀/子 pk=左
  pk/kind="link" 血缘/整批拒绝/`IS` 连接；`derive_refresh` link 分发
- [x] 1.2 引擎测试：成功物化（命中/未命中 NULL/撞名前缀）、列漂移指名、
  child 重名拒、refresh 重连、超限整批拒绝；既有测试全绿
- [x] 1.3 commit 并 push public/internal-main（只点本工具文件与测试）

## 2. api（fd-wire）

- [x] 2.1 ingest 常量 TOOL_LINK + entities 路由 materialize 端点（关系解析/
  双 cell 闸/列漂移 422/子集登记+回填/错误矩阵照 design D2）
- [x] 2.2 pytest：物化成功（假引擎断言载荷）/免费档任一端 403/关系或数据集
  失存 404/列漂移 422/旧引擎工具缺失如实/零半写

## 3. web（fd-wire）

- [x] 3.1 关系行「物化连接」动作（双 cell 才显示、成功跳数据集、错误逐条）
  + i18n + 教程物化小节 + vitest

## 4. 验证与发布（主线程）

- [x] 4.1 门禁：引擎 pytest / api pytest / web vitest+tsc / openspec
  validate --strict 零 error
- [x] 4.2 发布：引擎 internal-main push → fd-wire 快照仓出片（三镜像）→
  manifest 滚 api+web → drill cell env-file 法滚新 cell 镜像
- [x] 4.3 冒烟：drill cell MCP 层 link 实弹（成功/撞名/NULL/refresh）+ 公网
  api 冒烟（有 cell 档客户则端到端）；归档 change + 记忆
