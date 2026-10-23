# wire-artifact-marketplace 生产滚版报告（2026-10-14）

## 出片通道

law-ai-official 快照线（用户既定正道，禁本机构建）：

- 主仓 github/main 干净 clone（HEAD `4146d1b`）→ rsync 锚定 `/.git` →
  快照仓 commit `68242b8`（commit message 标注 `fd-wire @ 4146d1b`）。
- GHA run `38466793614`：**2 分 20 秒全绿**（api/web/cell + connector 五平台）。
- relay 回灌：cheap-3 cron 轮完成 hkccr → ccr；`skopeo inspect --creds`
  核验 `fd-wire/api:sha-68242b8` 与 `fd-wire/web:sha-68242b8` 双 OK。

## 迁移（备份先行——吸取 console-ux-fixes 教训）

1. 备份：cheap3 wire-postgres 容器内 pg_dump 双库
   （`wire_platform` 3.1MB / `wire_datasets` 36KB，gz，/tmp 留档）。
2. **迁移前置核查发现口径修正**：此前记录「wire_platform 0034 / wire_datasets
   0035」；本次逐一核验实为**双库均 0035**（0035 quality_rules 的 DDL 曾在
   双库同跑）。
3. `alembic upgrade head` 依次对两库执行（Mac → tailnet 100.64.0.12:5433，
   wire 角色；口令经临时 0600 文件传递、用毕即删）：
   - `wire_platform` 0035 → 0036 ✓
   - `wire_datasets` 0035 → 0036 ✓
4. 核验：两库 `alembic_version=0036_artifact_marketplace`；`artifacts` 六新列
   在位（visibility 默认 `'private'`、allow_copy 默认 false）；三新表
   （`artifact_market_subscriptions` / `artifact_share_grants` /
   `artifact_access_requests`）在位；全库 `visibility='market'` 计数 0
   （默认私有证明，存量 5 产物全部保持 private）。

## 滚版

- 清单：`deploy/k8s/fd-wire-api.yaml` + `fd-wire-web.yaml` → `sha-68242b8`
  （注释标真 sha 4146d1b），commit `2c9ffa4`，双推 gitee + github。
- ArgoCD app `fd-wire`（tracking fd-wire 仓 deploy/k8s）：hard refresh →
  自动 sync 至 `2c9ffa412c9a`，api/web rollout 双成功，双 pod 1/1 Running
  跑 `sha-68242b8`。

## 生产冒烟（pod 内 TestClient + dependency_overrides 注真客户；全绿）

owner=aloadtree@gmail.com（真看板 #1「E2E 宏观看板」v11）；
consumer=am-smoke-consumer（幂等新建）：

1. 缺省可见性 `private`（published 产物从未开放）✓
2. `PUT market/open/allow_copy=true` → 200，published_at/published_version=11 ✓
3. consumer 市场列表含「E2E 宏观看板」（SQL 层 market 过滤）✓
4. 详情结构摘要：panels=[{id,type,title}]，**无 snapshot/config/query** ✓
5. 订阅 200（created=true）→ 渲染 200：`access_source=market_subscription`，
   素材含快照（Q1 语义：公开看板=发布时刻数据快照）✓
6. **复制 200**：新产物 #7 draft，binding_report={bound:0, unbound:1,
   unbound_datasets:[e3428850…]}（consumer 对发布方数据集无访问权 → 诚实
   unbound 不赋权）；`copied_from_artifact_id=1` 溯源 ✓
7. **副本 spec 递归零 `snapshot` 键**（Q4 剥离实证）；json 全文无原快照值 ✓
8. 撤回（回 private）→ 原订阅者渲染 **403 如实**「已被发布方撤回或下架」✓
9. 清理：副本归档（200，无 delete 端点——归档即终态）+ 退订 200；
   库侧终态：非 private 产物 0、订阅 0、副本 archived 且 provenance 保留 ✓

`refreshed_at=None` 为设计内诚实回落（#1 板自上线未跑过刷新——旧版本无该键
→ 前端显「未知」不猜）；契约/AI 刷新路径写戳由 23 条后端用例覆盖。

## 公网面

- `GET /api/v1/market/artifacts`、`GET /api/v1/artifacts/1/visibility`
  未鉴权均 401 ✓
- web bundle `index-BExF1m7O.js` 含 3 处产物市场标记
  （artifact-visibility-option / market-artifact）✓

## 遗留

- 浏览器级目检（四档选择器 + 三页签 + 复制按钮条件）留给用户下次登录；
  前端 26 用例（选择器 9 + 市场页 17）已覆盖交互矩阵。
- 冒烟副本 #7（archived）与 am-smoke-consumer 客户行为审计留痕，按既定
  冒烟纪律保留（artifact 生命周期无 delete——归档即终态）。
