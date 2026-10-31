## MODIFIED Requirements

### Requirement: 产物可见性档位

客户 SHALL 能为单个产物（kind ∈ {dashboard, research_report}）选择**可见性
档位**：`private`（默认）| `link` | `directed` | `market`，并选择**开放模式**
`open`（订阅即可看）| `approval`（需发布方批准），以及**允许复制**开关
（`allow_copy`，默认关）。`artifacts` SHALL 记录 `visibility`（默认
`private`）、`access_mode`、`published_at`、`published_version`、`allow_copy`。
设置 SHALL 受闸：仅 owner/editor 可设（viewer 403）；仅 `published` 状态的
产物可设非 private 档位（draft/archived 拒绝并说明）；**绑定数据集含
「敏感」档时 SHALL 禁止设 market（422，允许 link/directed）**——「含」按
**有效敏感度**裁决（自身声明或祖先闭包继承，与数据集面同源同一 helper，
见 wire-asset-catalog「敏感度闭包闸与向下传播撤回」），错误文案 SHALL 如实
指名敏感祖先链；设置 SHALL 幂等且 append-only 留痕（`status_history` 追加
事件，含 before/after）；SHALL 可随时改回 `private`（撤回）。

#### Scenario: 发布看板到市场
- **WHEN** owner 对某 published 看板设置 `visibility=market`、`access_mode=open`
- **THEN** 设置成功并留痕；另一登录客户在市场检索其标题
- **THEN** 返回该产物条目（kind/标题/描述/版本/面板摘要，**不含数据值**）

#### Scenario: 草稿不可发布
- **WHEN** owner 对 draft 产物设置非 private 档位
- **THEN** 请求被拒（409/422 带说明「先发布再开放」）

#### Scenario: 默认私有
- **WHEN** 客户从未设置可见性
- **THEN** 该产物不出现在任何市场列表或检索结果中

#### Scenario: 敏感祖先链阻断发布

- **WHEN** 看板绑定一个自身「常规」、但祖先闭包含「敏感」的派生数据集，
  owner 将该看板设为 `market`
- **THEN** 请求被拒（422），错误如实指名该数据集及其敏感祖先链；
  设为 `link`/`directed` 不受影响

#### Scenario: 撤回即时失效
- **WHEN** 发布方把市场档产物改回 `private`
- **THEN** 该产物立即从市场列表与检索消失，已订阅者的渲染请求被拒

#### Scenario: viewer 不可设
- **WHEN** viewer 角色成员尝试设置可见性
- **THEN** 请求被拒（403）

### Requirement: 只读看（订阅者渲染）

订阅者 SHALL 能查看已订阅产物的**版本 spec 快照**（看板渲染为既有
DashboardRenderer 纯前端读 spec——**零引擎调用、零计量消耗**）。
订阅者可见内容 = 发布时刻的数据快照（**Q1 裁决：公开看板/报告包含发布
快照，文案须如实披露**）。渲染页 SHALL 显著展示快照时间（缺失时如实显
「未知」），SHALL NOT 自动触发刷新（未经发布方同意不得消耗其额度）。
未订阅者 SHALL 被拒（403）；撤回后 SHALL 立即失效。绑定数据集在渲染时
命中「敏感」档 SHALL 拒绝渲染（Q5 双点闸）——「命中」按**有效敏感度**
裁决（自身声明或祖先闭包继承，发布后被祖先敏感化连带的绑定数据集同样
命中；与数据集面同源同一 helper）。

#### Scenario: 订阅者渲染
- **WHEN** 已订阅客户打开某市场看板的渲染面
- **THEN** 返回该看板版本 spec（含发布时刻快照）与快照时间标注；
  零引擎调用（不消耗发布方计量）

#### Scenario: 未订阅被拒
- **WHEN** 未订阅客户请求渲染某市场看板
- **THEN** 请求被拒（403）

#### Scenario: 敏感度双点闸
- **WHEN** 发布方将绑定「敏感」档数据集的看板设为 market
- **THEN** 请求被拒（422）；若事后绑定数据集被改为「敏感」，
  订阅者渲染请求 SHALL 被拒（不静默出数据）

#### Scenario: 渲染复检含祖先闭包

- **WHEN** 看板发布后，其绑定的某派生数据集的祖先被改为「敏感」
  （该数据集自身未标注）
- **THEN** 订阅者渲染请求被拒（不静默出数据），文案指名敏感祖先链；
  该祖先改回「常规」后渲染恢复

#### Scenario: 看板公开不等于数据公开
- **WHEN** 订阅者渲染看板后尝试经数据面直接查询该看板绑定的发布方数据集
- **THEN** 仍按数据集市场规则处理（未订阅该数据集 → 拒绝）——两条闸独立
