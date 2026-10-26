# HANDOFF: wire-indicator-to-dataset — 剩余工作交接（2026-10-24）

> 接手前先读：`proposal.md` / `design.md` / `tasks.md`（本目录）。
> 上游 change `business-mcp-indicator-series-read`（finddata 根 openspec）
> **已全部完成并上产**，本 change 消费它的 `indicator_series_read` 工具。

## 已完成（代码已全部提交并推送三远端）

- **任务 1.1–1.6 全部完成**：建集第四态 `source.kind="indicator"`（schemas /
  routes 分派 / `indicator_fetch.py` 拉数模块 / 溯源 `dataset.indicator.registered`
  / 双档配额闸 / 负向面）。测试 `api/tests/test_datasets_indicator.py` 22 用例，
  api 全量 1461 绿。
- **任务 2.1–2.4 全部完成**：目录详情页 verified 分支换「落成数据集」表单
  （`web/src/views/CatalogDetailView.tsx` 的 `IndicatorToDatasetCard`）；
  数据集列表「指标目录」琥珀徽标 + 筛选项；i18n zh/en 各 13+1 键。web 1022
  绿、tsc 零错。提交 `50addfa`，docker 适配层 `ee262bc`+`9016390`。
- **生产已滚**：fd-wire api=`sha-dd72128`、web=`sha-05c57cc`（build-pool 出片，
  ccr 两 tag 已验）。**注意：滚版 commit 未落** —— `deploy/k8s/fd-wire-api.yaml:46`
  与 `fd-wire-web.yaml:32` 的 tag bump 还在工作树未提交（git status 可见两个
  M 文件）。**第一件事：核对这两行后 commit + push gitee**（ArgoCD app
  `fd-wire` source = 本仓 gitee main 的 `deploy/k8s/`，selfHeal 开着，push 即滚）。

## 剩余工作（按序）

### 1. 提交滚版 + 生产冒烟（任务 3.3）

- commit 信息建议：`roll(prod): fd-wire api→sha-dd72128 web→sha-05c57cc（指标落成数据集）`。
  yaml 注释里旧「testaction002 快照 sha=f07ed95」行顺手更正为新 tag 出处。
- 滚后验收：`kubectl --context cheap -n fd-prod rollout status deploy/fd-wire-api`
  与 `fd-wire-web`；公网 `https://wire.finddatatech.cloud/health` 200。
- **冒烟（真账号 aloadtree 或 platform-test，免费档 + cell 档各一轮）**：
  1. 目录（#/catalog）挑一条 **verified** 指标 → 详情页「落成数据集」→
     填实体/范围提交 → 跳数据集详情；
  2. 行数与上游对账：抽 3 行 date/value 对照（上游实调
     `indicator_series_read`，生产 pod 内可打，zihan `fd-mcp` ns 部署）；
  3. 该数据集 derive 派生 → 看板绑数 → 溯源账本查 `dataset.indicator.registered`；
  4. 重复提交同指标 → 409 带既有 key；registered 指标 → 422 导流文案；
  5. 免费档超行数闸 → 429 整批拒绝、零残留。
- ⚠️ **生产 `INDICATOR_FETCH_URL` 未配置**（本 change 新 env，未进
  `fd-wire-env` secret）。缺省值指向 `http://fd-find-data-business-mcp.fd-mcp.svc:8310`
  ——但 business-mcp 部署在 **zihan 集群 fd-mcp ns**（cheap 的 fd-prod 无此
  svc），跨集群解析不了。冒烟前必须二选一：
  - **推荐**：走 zihan NodePort 30803（svc `fd-find-data-business-mcp`
    ClusterIP 10.43.192.120:8310，NodePort 见其 manifest）配
    `INDICATOR_FETCH_URL=http://<cheap节点可达的zihan入口>:30803/mcp`；
    走网关则 `https://mcp.finddatatech.cloud/fd-find-data-business-mcp/mcp` +
    `INDICATOR_FETCH_TOKEN`（Logto JWT 或 FDBIZ_INTERNAL_TOKEN 同值——后者
    是服务间固定密钥轨，配在 fd-wire-env secret 加 `indicator_fetch_token` 键），
    再在 deploy/k8s/fd-wire-api.yaml env 块加两个 env 条目（照 SCOPE_API_BASE
    的注释风格）；
  - 或在 cheap fd-prod 起一个 ExternalName/手写 svc 指向 zihan 入口。
  **fetch 模块带 `/mcp` 后缀自动补齐**（`mcp_url` property），URL 带不带都可。

### 2. 任务 3.1/3.4（轻）

- 3.1 的 api/web 测试与 PG 腿已绿（1461 全量含 PG 方言雷区用例；datasets
  路径双方言断言在既有套件），只差「生产冒烟」即 3.3 的内容，勾掉即可。
- 3.4：`openspec validate wire-indicator-to-dataset --strict` —— CLI 1.12
  对中文 spec 误报 RFC2119 已知，以零 error 为门；归档前 tasks 与测试对账。

### 3. 已知坑（都咬过，别再踩）

- **环形导入假象**：直接 `import fd_wire_api.datasets.ingest` 在当前 HEAD
  就报 circular（预存在，非本 change 引入）；走 `fd_wire_api.app` 或 pytest
  全量入口即过。单文件直跑测试会假红。
- **`fd-build-snapshot` 从 origin 干净 clone**（不是工作树）——fd-wire 的
  `origin`=github 镜像，**push 完 gitee 必须 `git push github main`**，否则
  快照缺最新提交（本次 docker/ 适配层就是这样漏的）。
- **通用构建池模板 `context: .` 硬编码**：wire 的 api/web Dockerfile 是
  子目录相对 COPY，故仓里加了 `docker/api.Dockerfile` / `docker/web.Dockerfile`
  适配层（COPY 路径带 `api/`、`web/` 前缀；多源 COPY 每个源都要前缀——
  web 那个修过一次）。改子目录 Dockerfile 时同步适配层。
- **cell 镜像本批未出**（引擎未动）。滚 cell 才需要出 cell 镜像（依赖
  fd-daas-mcp named context，池模板不支持 build-contexts，需另桥）。
- **池 smoke gate**：wire api 容器无业务 env 时 lifespan 建不了 SQLite →
  boot/http 门必假红。两池已设 `SMOKE_MODE=skip`，健康验收在部署侧做。
- **FDT org Actions 计费墙**（scs001 和 FindDataTechnology 都耗尽）：私有仓
  CI 全灭，出片一律走 build-pool（testaction004=api、testaction003=web 已配好
  变量；月初额度重置，可用 `fd-build-channel` 查池余额轮换）。
- 分类器/网络抖动重试即可，别改流程。

### 4. 归档

- 冒烟全绿 + tasks 全勾 → `openspec archive wire-indicator-to-dataset`
  （fd-wire 根 openspec）。上游 change `business-mcp-indicator-series-read`
  已在 finddata 根归档就绪（10/10 全勾），可在那边一并 archive。
- 顺手：上游 tasks 3.1 里「网关注册工具面刷新」——网关 registry 是否已把
  `indicator_series_read` 列入 fd-find-data-business-mcp 工具面，用
  MCP 实调配方（registry pod 内 initialize+tools/list）核一眼。

## 关键坐标速查

| 项 | 值 |
|---|---|
| 本 change 目录 | fd-wire/openspec/changes/wire-indicator-to-dataset/ |
| 上游 change | finddata/openspec/changes/business-mcp-indicator-series-read/（10/10 完成） |
| 上游生产镜像 | zihan fd-mcp ns，`sha-1d4d61b` 之后滚的 sha-ac69046 |
| api/web 新 tag | ccr `fd-wire/api:sha-dd72128`、`fd-wire/web:sha-05c57cc` |
| 滚版入口 | deploy/k8s/fd-wire-{api,web}.yaml（已改未提交）+ push gitee |
| 测试 | api: `cd fd-wire/api && .venv/bin/python -m pytest tests/ -q`；web: `cd fd-wire/web && npx vitest run` |
