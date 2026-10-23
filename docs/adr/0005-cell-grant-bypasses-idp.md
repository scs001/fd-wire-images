# Cell grant groups live outside the IdP (synthetic groups in key snapshot + scope doc only)

cell 授权的 per-customer 组（`wire-cust-<customer_id>`）只在两处存在：客户长命 key 的组快照、registry 侧 scope 文档的 `group_mappings`。不在 Logto（或任何 IdP）建组、不进 IdP 组管理面——组到 scope 的解析是 registry 对自己 Mongo 的实时 `$in` 查询，从不校验组在 IdP 存在。这样选是为了让授权束随开户全自动铸造/回收（开一户不需要在 IdP 建组、退一租不需要删组），且机器 key 轨道的信任链根本不经过 IdP；一旦客户规模上来，组名约定就铸进了每一把 key，回退要重铸全部客户凭据，故趁早定案。

## Considered Options

- **IdP 托管 per-customer 组**（Logto 每客户建组、key 快照取自 IdP 组成员关系）：身份面单一，但 Logto M2M 组管理 API 曾 404（仅 Admin Console UI 可用），「开一户动一次 IdP」与全自动闭环相悖；且 wire 客户全部走机器 key，没有会话身份去消费 IdP 组。否决。
- **合成组不经 IdP**（本决策）：组是授权束的内部记号，铸造时写进快照即固化；Logto 只保留共享组 `wire-customers`（映射共享业务面 scope）。

## Consequences

- 组名空间 `wire-cust-<customer_id>` 专属本轨道：将来任何人若真在 Logto 建同名组，也不会影响快照轨道的语义（解析只看 scope 文档 `group_mappings`），但应避免混淆，不要建。
- scope 文档的特权写防护继续兜底：`wire-cust-*` 永远不能映射到 admin scope（`mcp-registry-admin`）。
- 若未来出现「客户本人在 registry 控制台登录」的需求（浏览器会话身份），那时组快照与 IdP 组的合流需要重新设计——那是新前提，不是本决策的漏洞。
