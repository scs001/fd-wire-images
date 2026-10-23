## Why

产物 HTTP 面自 wire-dashboard-blank-create（2026-10-11）起已有 `POST /api/v1/artifacts`，
但只收 `{kind,title}` 内造最小空 spec——**创建不能携带内容**。程序化发布（省级面板
看板、行业看板等平台自营批量上架）因此进不了 HTTP：`import_showcase.py` 只能跑在
pod 内直调 service 层并直连双库（SQLite PVC），发布流程绑死 `kubectl exec` 与集群
访问；重复执行靠脚本内扫 `spec.showcase_slug` 查重，幂等约定没有 HTTP 面支撑。
2026-10-22 省级看板导入时用户定性：能管不能建（准确说：能建空白、不能带内容）
是设计缺陷，应有开放接口。

## What Changes

- `POST /api/v1/artifacts` 的入参 `ArtifactCreateIn` 增加可选 `spec` 字段：带 spec
  的创建一步生成「带内容 draft」（v1=给定 spec，走与 PUT 相同的 `validate_kind_spec`
  分派校验）；不带 spec 保持既有空白创建行为逐字节不变（全租户可用）。
- **红线**：带 spec 内容创建仅限平台租户（`finddata-official`，即平台自营发布方）；
  普通租户带 spec 创建 403。空白创建权限不动。市场/橱窗可见性门槛全部不动
  （橱窗查询层已有 `customer_id == finddata-official` 硬条件，visibility 仍需
  显式 PUT 且过 `require_asset_editor` 门）。
- 幂等键：沿用 `import_showcase` 既有的 `spec.showcase_slug` 约定，升格为平台契约
  ——同租户内同 slug 的重复 POST 不建重，走追加版本（append-only，语义与 PUT
  一致）并返回既有产物；跨租户同 slug 互不干扰。
- `import_showcase.py` 增 HTTP 客户端模式（POST 带 spec+slug → publish →
  visibility 全走开放端点），解除对 `kubectl exec` 与 pod 内直连双库的依赖；
  pod 直跑模式保留为回退。
- 非目标：不改空白创建权限（不做「登录即可建内容板」的放开，如需放开另议）；
  不改可见性/市场模型；不给 slug 加数据库列（v0 遍历平台租户产物查重，量级
  几十条）。

## Capabilities

### New Capabilities

（无——全部落在既有能力的 delta 上）

### Modified Capabilities

- `artifact-lifecycle`: 产物创建面新增「带内容创建（程序化发布通道，限平台租户）」
  与「showcase_slug 幂等」两组需求；空白创建既有行为不变。
