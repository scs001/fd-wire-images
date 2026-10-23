# artifact-marketplace Delta

## ADDED Requirements

### Requirement: 产物可见性档位

客户 SHALL 能为单个产物（kind ∈ {dashboard, research_report}）选择**可见性
档位**：`private`（默认）| `link` | `directed` | `market`，并选择**开放模式**
`open`（订阅即可看）| `approval`（需发布方批准），以及**允许复制**开关
（`allow_copy`，默认关）。`artifacts` SHALL 记录 `visibility`（默认
`private`）、`access_mode`、`published_at`、`published_version`、`allow_copy`。
设置 SHALL 受闸：仅 owner/editor 可设（viewer 403）；仅 `published` 状态的
产物可设非 private 档位（draft/archived 拒绝并说明）；**绑定数据集含
「敏感」档时 SHALL 禁止设 market（422，允许 link/directed）**；设置 SHALL 幂等且
append-only 留痕（`status_history` 追加事件，含 before/after）；SHALL 可随时
改回 `private`（撤回）。

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

#### Scenario: 撤回即时失效
- **WHEN** 发布方把市场档产物改回 `private`
- **THEN** 该产物立即从市场列表与检索消失，已订阅者的渲染请求被拒

#### Scenario: viewer 不可设
- **WHEN** viewer 角色成员尝试设置可见性
- **THEN** 请求被拒（403）

### Requirement: 产物市场浏览与检索

登录客户 SHALL 能检索市场档产物：按标题/描述关键字、kind、标签筛选与分页。
列表 SHALL 在 SQL 层只取 `visibility='market'` 的行。详情 SHALL 返回
元数据与**面板结构摘要**（逐面板仅 `{id, type, title}` 三字段——
SHALL NOT 含 config/snapshot/query）；完整 spec（含快照）仅订阅后经渲染面
可得。未鉴权 SHALL 被拒。

#### Scenario: kind 筛选
- **WHEN** 客户在市场按 kind=dashboard 筛选
- **THEN** 只返回看板类条目（研究报告不在结果中）

#### Scenario: 未鉴权被拒
- **WHEN** 未携带有效身份的请求访问市场产物端点
- **THEN** 请求被拒（401）

#### Scenario: 详情不含数据与查询
- **WHEN** 未订阅客户查看某市场产物详情
- **THEN** 返回体只含元数据与 `{id, type, title}` 面板清单，
  **不含 config/snapshot/query 任何字段**，更无数据值

#### Scenario: 公开面如实披露快照
- **WHEN** 查看市场详情或渲染页
- **THEN** 界面显著标注「公开看板/报告包含发布时刻的数据快照」
  （SHALL NOT 宣称「仅结构不含数据」）

### Requirement: 订阅与审批

市场档产物 SHALL 支持订阅（`open` 档订阅即得、`approval` 档需发布方批准）。
订阅 SHALL 授予订阅者**只读看**权（渲染该产物）。发布方 SHALL 能查看待批
请求并批准/拒绝；批准 SHALL 幂等地建立订阅。定向档（directed）SHALL 经
授权名单授予同一只读看权。

#### Scenario: 订阅即得
- **WHEN** 客户订阅某 open 档看板
- **THEN** 订阅关系建立，后续可渲染该看板

#### Scenario: 审批流
- **WHEN** 客户申请某 approval 档看板，发布方批准
- **THEN** 订阅关系建立；再申请同一产物 SHALL 幂等（不重复建行）

#### Scenario: 拒绝留痕
- **WHEN** 发布方拒绝某申请
- **THEN** 申请状态为 rejected，申请方可重新申请

### Requirement: 只读看（订阅者渲染）

订阅者 SHALL 能查看已订阅产物的**版本 spec 快照**（看板渲染为既有
DashboardRenderer 纯前端读 spec——**零引擎调用、零计量消耗**）。
订阅者可见内容 = 发布时刻的数据快照（**Q1 裁决：公开看板/报告包含发布
快照，文案须如实披露**）。渲染页 SHALL 显著展示快照时间（缺失时如实显
「未知」），SHALL NOT 自动触发刷新（未经发布方同意不得消耗其额度）。
未订阅者 SHALL 被拒（403）；撤回后 SHALL 立即失效。绑定数据集在渲染时
命中「敏感」档 SHALL 拒绝渲染（Q5 双点闸）。

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

#### Scenario: 看板公开不等于数据公开
- **WHEN** 订阅者渲染看板后尝试经数据面直接查询该看板绑定的发布方数据集
- **THEN** 仍按数据集市场规则处理（未订阅该数据集 → 拒绝）——两条闸独立

### Requirement: 可复制（spec 复制与绑定重映射）

发布方开启 `allow_copy` 时，订阅者 SHALL 能复制该产物到自己账号，成为
自己的 draft（version=1，spec 独立）。**复制 SHALL 剥离全部面板快照
（`snapshot` 键递归移除）**——复制者是拿到分析结构（布局/图表配置/查询
绑定），不是数据（Q4：数据仍需单独授权）。复制时看板面板的 `dataset_id`
绑定 SHALL 重映射：调用方对数据集有访问权（自有/已订阅/定向授权）→
保留绑定；无访问权 → 该面板标 `unbound`（**不自动赋权、不落假数据**）。
复制 SHALL 留 `copied_from_artifact_id` 溯源指针（仅审计，无更新提示）。
复制响应 SHALL 带逐面板绑定报告。`allow_copy=false` 时复制请求 SHALL
被拒（403）。

#### Scenario: 全绑定复制
- **WHEN** 订阅者已订阅发布方的看板及其全部绑定数据集并执行复制
- **THEN** 新产物为 draft，全部面板绑定保留，报告 bound=n/unbound=0
- **AND** 新 spec 中**不存在任何 snapshot 键**（快照已剥离），
  看板初始呈现「无数据」如实态

#### Scenario: 部分未绑定诚实标注
- **WHEN** 订阅者只订阅了看板（未订阅绑定数据集）并执行复制
- **THEN** 复制成功，相关面板标 `unbound`，报告如实计数；编辑器提示需绑定

#### Scenario: 不允许复制
- **WHEN** 发布方未开启 `allow_copy`，订阅者尝试复制
- **THEN** 请求被拒（403）并说明发布方未允许复制

#### Scenario: 复制后独立
- **WHEN** 复制者编辑自己复制的产物
- **THEN** 不影响发布方原产物（spec 相互独立）

#### Scenario: 溯源指针
- **WHEN** 任一客户复制某产物
- **THEN** 新产物带 `copied_from_artifact_id`（指向源产物，仅审计用）

### Requirement: 既有签名链接路径不受影响

本能力的市场面 SHALL NOT 改变既有签名分享链接的行为（HMAC + TTL +
版本钉死 + 免登录）：两条通道并存（链接=点对点临时、市场=登录客户可发现），
对同一产物的链接生成/打开 SHALL 与本能力引入前逐字一致。

#### Scenario: 链接行为零回归
- **WHEN** 对已发布产物生成分享链接并打开
- **THEN** 行为与本能力引入前一致（版本钉死、TTL 生效、免登录可看）
