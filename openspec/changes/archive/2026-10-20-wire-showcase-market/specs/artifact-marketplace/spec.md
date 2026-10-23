## MODIFIED Requirements

### Requirement: 产物市场浏览与检索

登录客户 SHALL 能检索市场档产物：按标题/描述关键字、kind、标签筛选与分页。
列表 SHALL 在 SQL 层只取 `visibility='market'` 的行。详情 SHALL 返回
元数据与**面板结构摘要**（逐面板仅 `{id, type, title}` 三字段——
SHALL NOT 含 config/snapshot/query）；完整 spec（含快照）仅订阅后经渲染面
可得。未鉴权 SHALL 被拒。

**匿名例外（wire-showcase-market）**：平台自营发布方的 `market` 档看板另经
独立的匿名只读橱窗端点对外（见 `showcase-market` 能力）。本要求的登录门禁
MUST 对客户发布方继续无条件生效——匿名橱窗 MUST NOT 作为访问客户产物的替代
通道；客户产物在任何匿名响应中均不可见、不可计数、不可推断存在性。

市场条目 SHALL 标识发布方是否为平台自营，使登录客户能区分平台自有内容与
其他客户发布的内容。

#### Scenario: kind 筛选
- **WHEN** 客户在市场按 kind=dashboard 筛选
- **THEN** 只返回看板类条目（研究报告不在结果中）

#### Scenario: 未鉴权被拒
- **WHEN** 未携带有效身份的请求访问市场产物端点
- **THEN** 请求被拒（401）

#### Scenario: 匿名橱窗不构成客户产物通道
- **WHEN** 匿名访客只访问橱窗端点
- **THEN** 无法取得任何客户发布方的产物（列表、详情、计数皆无）

#### Scenario: 平台自营标识
- **WHEN** 登录客户在市场浏览条目
- **THEN** 平台自营发布方发布的条目被明确标识，与其他客户条目可区分

#### Scenario: 详情不含数据与查询
- **WHEN** 未订阅客户查看某市场产物详情
- **THEN** 返回体只含元数据与 `{id, type, title}` 面板清单，
  **不含 config/snapshot/query 任何字段**，更无数据值

#### Scenario: 公开面如实披露快照
- **WHEN** 查看市场详情或渲染页
- **THEN** 界面显著标注「公开看板/报告包含发布时刻的数据快照」
  （SHALL NOT 宣称「仅结构不含数据」）
