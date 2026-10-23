# transform-pipeline Specification

## Purpose
TBD - created by archiving change wire-transform-pipeline-view. Update Purpose after archive.

## Requirements

### Requirement: 管线图端点

登录客户可经 console 会话获取本企业数据集派生图
（`GET /api/v1/pipeline/graph`）：nodes 为本企业全部数据集（id/key/名称/
source_kind/storage_tier/行数/更新时间/有无定时重派生登记），edges 为
父→子派生边（本地血缘元数据 source_ref；操作标注 v0 统一为 derived——
derive/join 区分未入本地登记），节点
带拓扑深度标注（源=0）；**零引擎调用**（纯本地元数据组装）；跨企业隔离
（他企数据集不出现）；环防护（异常环边跳过并计入 anomalies）；节点上限
500、超出如实返回 truncated。

#### Scenario: 派生链成图
- **WHEN** 客户拥有上传数据集 A 与其派生 B、B 的 join 派生 C 并发起请求
- **THEN** 返回三节点两边的图，A 深度 0、B 深度 1、C 深度 2，边操作标注为 derived

#### Scenario: 无派生空图
- **WHEN** 客户只有上传数据集、无任何派生
- **THEN** 返回 nodes 含上传数据集、edges 为空

#### Scenario: 企业隔离
- **WHEN** 另一企业存在数据集与派生链
- **THEN** 图中不出现他企任何节点或边

#### Scenario: 环异常防护
- **WHEN** 血缘元数据出现环（异常数据态）
- **THEN** 环边被跳过、anomalies 计数如实返回，端点不报错

### Requirement: 管线视图

数据页提供「管线」子视图渲染派生图：按拓扑深度分列布局（源在最左、派生
逐级向右），节点卡呈现名称/行数/更新时间/tier 徽标/调度徽标/kind 标注，
SVG 连线父→子；点击节点跳该数据集详情页；无派生数据集时空态如实提示；
窄屏退化为按深度分组的卡片列表。v0 只读——视图上不提供刷新或派生操作
（操作入口在数据集详情页）。

#### Scenario: 管线可视化
- **WHEN** 客户打开管线视图且存在派生链
- **THEN** 按深度分列呈现节点卡与父→子连线，节点卡信息完整

#### Scenario: 空态诚实
- **WHEN** 客户没有任何派生数据集
- **THEN** 显示空态提示（引导从数据集详情发起派生），不显示空画布或报错

#### Scenario: 节点跳转
- **WHEN** 客户点击某节点卡
- **THEN** 跳转到该数据集的详情页

### Requirement: 控制台重派生与定时管理

派生数据集详情页（derived + cell 档）提供「重派生」按钮：级联默认开可关，
调用既有重派生端点（console 会话凭据），级联各级结果（数据集名/深度/
成功失败与原因）如实呈现，刷新后行数与更新时间同步；并提供「定时重派生」
登记管理：cron 五字段登记/更新、当前登记态（cron/enabled/最近运行时间/
状态/错误/行数）如实呈现、删除登记，naive UTC 口径诚实注记。api 层
（免费档）数据集不出现该入口；管线视图保持只读。

#### Scenario: 手动重派生即级联
- **WHEN** 客户对派生数据集点击重派生（级联开）
- **THEN** 本体与派生子链依次刷新，级联各级结果如实呈现，行数/更新时间同步

#### Scenario: 定时登记管理
- **WHEN** 客户登记 cron 后查看
- **THEN** 登记态与最近运行结果如实呈现；删除后回到未登记态

#### Scenario: 免费档无入口
- **WHEN** 数据集是 api 层（免费档）
- **THEN** 详情页不出现重派生与定时管理入口
