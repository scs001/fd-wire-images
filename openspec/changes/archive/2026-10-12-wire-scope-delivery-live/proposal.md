# wire-scope-delivery-live

## Why

需求单交付承诺「交付时自动开客户私有 scope（失败可重试）」——wire 侧机制自
wire-platform-v1 任务 6.6 就在，但生产从未点亮：pod 无 `SCOPE_API_BASE`，
两张已交付单 scope_status 恒 pending。上游 fd-open-data-mcp 的 REST scope 面
由伴生 change `scope-rest-api`（该仓）补齐后，本 change 把 wire 侧接通：

1. **重试端点缺失**：`deliver_demand` 的重试语义（delivered+pending 再调只重试
   建 scope）没有任何路由暴露——transition 端点只在 in_progress→delivered
   迁移时挂钩子，存量 pending 单无路可补课；
2. **配置接线**：SCOPE_API_BASE/SCOPE_API_TOKEN 落部署清单与 secret；
3. **运营面**：管理台对 delivered+pending 单需要「重试开 scope」动作（现状
   运营只能看不能补）。

## What Changes

- **api**：新增 `POST /internal/demands/{demand_id}/scope-retry`（内网令牌）与
  管理台同语义端点——delivered 且 scope_status=pending 才受理（409 如实回显
  其余状态），内部复用 `deliver_demand` 既有重试分支（不迁移状态、只重试建
  scope+逐指标 grant，失败照旧留档 pending）；请求可带可选
  `indicators`/`source_dbs` 覆盖（缺省用 linked_indicators）。
- **web（管理台）**：DemandsPage 对 delivered+pending 单呈现「重试开 scope」
  按钮（失败原因行旁），点击调上述管理端点、如实呈现结果（ok=绿标+scope 名，
  失败=新错误文案），i18n 双语。
- **部署**：`deploy/k8s/fd-wire-api.yaml` 加 `SCOPE_API_BASE`（fd-prod 内网
  `http://fd-open-data-mcp:8300`，清单字面值——内网地址非敏感）与
  `SCOPE_API_TOKEN`（secretKeyRef fd-wire-env/scope_api_token，与 mcp 侧
  同值）。
- **docs**：`deploy/README.md` 补「scope 交付接线」段（配置对、重试端点用法、
  演练记录位）。

## Design Decisions

- **重试走既有 service 分支**：不新写重试逻辑——`deliver_demand` 的
  delivered+pending 分支就是为此设计的（docstring 明言），端点只是暴露它；
  变异自证锁「非法状态 409」。
- **SCOPE_API_BASE 指向 fd-prod 内网服务名**：wire api 与 fd-open-data-mcp
  同集群同 ns（fd-prod），走 ClusterDNS 不出集群——与「平台侧永不直读 cell
  数据」等网络纪律不冲突（scope 面是 registry 治理元数据，非客户 cell 数据）。
- **空指标单的重试如实失败**：linked_indicators 空且无覆盖 → 空规则被上游
  400 拒（invalid_scope），wire 留档新失败事件——诚实降级而非虚构成功。

## Capabilities

### Modified Capabilities

- `demand-orchestration`: +2 要求（交付 scope 重试端点；运营面重试动作与
  配置接线）。

## Impact

- **api**：demands/routes.py（+scope-retry 端点）、admin 路由（同语义）、
  测试（重试矩阵：pending→ok / pending→再失败 / 非 delivered 409 / ok 态 409 /
  覆盖 indicators）。
- **web**：DemandsPage 按钮+api 客户端+i18n+测试。
- **deploy**：fd-wire-api.yaml env 两项；fd-wire-env secret 新 key。
- 零迁移、零新表。
