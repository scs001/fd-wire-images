# Tasks

## 1. api

- [x] 1.1 `POST /internal/demands/{id}/scope-retry`（内网令牌）：delivered+pending 受理，复用 deliver_demand 重试分支（indicators/source_dbs 可选覆盖）；非 delivered 409 / ok 态 409 如实回显
- [x] 1.2 管理台同语义端点（照 wire-demand-visibility 的 admin transition 惯例）
- [x] 1.3 测试：pending→ok（scope_created 留档+scope_name）、pending→再失败（留档+保持 pending）、in_progress 409、ok 409、indicators 覆盖生效、未配置 scope client 的诚实降级文案
- [x] 1.4 变异自证：放宽状态闸 → 409 用例红；恢复全绿

## 2. web（管理台）

- [x] 2.1 DemandsPage：delivered+pending 单「重试开 scope」按钮+结果呈现（成功=scope 名/失败=错误文案）；i18n zh/en；测试（按钮仅 pending 态出现、点击后状态流转、双语键）
- [x] 2.2 api.ts 客户端函数

## 3. 部署与文档

- [x] 3.1 `deploy/k8s/fd-wire-api.yaml`：SCOPE_API_BASE 字面值 + SCOPE_API_TOKEN secretKeyRef（fd-wire-env/scope_api_token）；secret 同步落集群（与 mcp 侧同值）
- [x] 3.2 `deploy/README.md`「scope 交付接线」段：配置对、重试端点用法、演练记录

## 4. 门禁与发布

- [x] 4.1 api pytest 全绿 / web vitest+tsc 0 / `openspec validate wire-scope-delivery-live --strict`
- [x] 4.2 双推→快照仓→CI→relay→滚版→ArgoCD（mcp 侧先行滚完再滚 wire）

## 5. 实弹演练与归档

- [x] 5.1 演练：带指标锚点的新演练单（linked_indicators=真实 verified code）in_progress→delivered → 期待 scope_status=ok 全链（上游建 scope+grant 可见）；存量空指标单重试 → 如实失败留档
- [x] 5.2 清场（演练单删除+mcp 侧 scope_delete）+ `openspec archive` + `validate --specs` + ROADMAP 方向 C 标注 + 记忆更新 + 快照仓同步
