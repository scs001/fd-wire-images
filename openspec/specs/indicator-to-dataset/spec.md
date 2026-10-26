# indicator-to-dataset Specification

## Purpose
指标目录 verified 条目一键落成客户数据集：经上游统一拉数工具（business-mcp
indicator_series_read）取序列，免费档 api 层本地物化、cell 档引擎 ingest 三段，
幂等/配额/如实报错口径与既有建集面一致。

## Requirements

### Requirement: verified 指标 SHALL 可落成客户数据集

用户 SHALL 能从指标目录的 verified 条目发起「落成数据集」：指定实体与时间范围后，平台经上游统一拉数工具取得序列并落成该客户名下的数据集。registered 条目 SHALL 维持需求单预填导流，SHALL NOT 提供拉数建集入口。

#### Scenario: verified 指标建集成功
- **WHEN** 登录客户对一条 verified 指标提交建集（合法实体与时间范围）
- **THEN** 平台经上游拉数并建集成功，数据集 `source_kind="indicator"`、`source_ref` 为该指标标识
- **AND** 行内数据品牌为 finddata，溯源账本记录建集事件（含指标标识/实体/范围）

#### Scenario: registered 指标拒建集并导流
- **WHEN** 对 registered 状态的指标提交建集
- **THEN** 请求被拒（422），文案指明该指标尚未认证并引导走需求单
- **AND** 该指标详情页不渲染「落成数据集」入口，维持既有需求单预填

### Requirement: 建集双档 SHALL 复用既有落地与配额模式

免费档建集 SHALL 在 api 层本地物化（零引擎调用），并受既有行数闸与存储闸约束——超限整批拒绝零半写。cell 档 SHALL 经引擎 ingest 三段落库，失败中止并清理，行数/存储计量照既有路径。拉数本身 SHALL NOT 记入客户消费计量（自家数据零计费）。

#### Scenario: 免费档超行数闸整批拒绝
- **WHEN** 免费档客户拉取的序列行数超过免费档行数上限
- **THEN** 建集被拒且不落任何半成品数据（本地事务回滚）
- **AND** 客户既有数据集与配额计数不变

#### Scenario: cell 档经引擎落库
- **WHEN** cell 档客户提交指标建集
- **THEN** 行经引擎 ingest 落客户 cell，计量照既有路径记录
- **AND** 引擎调用失败时无残留半写（ingest abort）

#### Scenario: 拉数不计消费账
- **WHEN** 任一档位完成指标建集
- **THEN** 消费计量侧不产生该次拉数的计量记录
- **AND** 客户侧行数/存储配额照常约束该数据集

### Requirement: 重复指标建集 SHALL 幂等拒绝并指路

同一客户对同一指标已存在活跃数据集时再次建集 SHALL 返回 409，响应携带既有数据集标识；SHALL NOT 自动覆盖、自动刷新或静默建第二集。

#### Scenario: 重复建集返回既有数据集
- **WHEN** 客户对已有数据集的指标再次提交相同建集
- **THEN** 返回 409，detail 携带既有数据集 key
- **AND** 不新建数据集、不改动既有数据集内容

### Requirement: 上游不可用 SHALL 如实报错零伪装

上游拉数工具不可达、超时或返回「不支持/解析失败」时，建集 SHALL 以对应语义报错（502/422），SHALL NOT 以空数据集、截断时间范围或部分行静默降级。时间范围 SHALL 有上限校验，超限 422。

#### Scenario: 上游不可达如实 502
- **WHEN** 上游统一拉数工具不可达或超时
- **THEN** 建集返回 502，不建数据集
- **AND** 错误如实描述上游不可用，不伪装为「无数据」

#### Scenario: 时间范围超限被拒
- **WHEN** 请求的时间范围超过上限
- **THEN** 建集被拒（422），不发起上游拉取
