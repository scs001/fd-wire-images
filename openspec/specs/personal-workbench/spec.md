# personal-workbench Specification

## Purpose
TBD - created by archiving change wire-personal-workbench. Update Purpose after archive.

## Requirements

### Requirement: 工作台聚合端点

平台 SHALL 提供工作台聚合端点 `GET /api/v1/workbench`（登录客户身份）：单次
往返返回本客户的数据集（总数、api/cell 分层计数、最近列表）、免费档真实配额
（付费档为 null）、源包订阅（计数与最近订阅）、产物（活跃总数、按类计数、
最近列表）、告警面（alert_rule 计数、firing 中计数、近 24h 事件数）与需求单
（进行中计数、最新一单摘要）。端点只读、免费档可用、读取不产生行数计量。

#### Scenario: 聚合一次返回

- **WHEN** 已登录客户请求 `GET /api/v1/workbench`
- **THEN** 六个 section（datasets/packs/artifacts/alerts/demands 及配额）在单次响应中如实返回，数字与各业务面一致

#### Scenario: 未登录拒绝

- **WHEN** 无有效登录会话请求该端点
- **THEN** 返回 401，不产生任何数据库读副作用以外的响应内容

### Requirement: 工作台跨客户隔离

工作台聚合响应 SHALL 仅含请求客户自己的数据：数据集、订阅、产物、告警事件、
需求单均按 customer_id 过滤；其他客户的任何标识不出现在响应体中。

#### Scenario: 隔离

- **WHEN** 客户 A 请求工作台而库中存在客户 B 的数据集/产物/订阅/需求
- **THEN** 响应各 section 仅统计与呈现 A 的条目，B 的名称/标识/计数零出现

### Requirement: 控制台工作台页

控制台 SHALL 提供「工作台」页签（侧栏首位）：聚合呈现各 section——计数、
最近条目（深链到既有页：数据集、源包、产物、需求）、每段空态含上手 CTA
（上传 CSV/浏览源包/去 AI 构建）；免费档配额以内联条呈现（限额触达内联升档
提示而非整页拦截）；页内含壹座 agent 入口紧凑卡，链接语义与「AI 构建」页
引导一致（主入口官网 demo 页、次级壹座正式入口），卡片不复述测试账号明文。

#### Scenario: 空态上手引导

- **WHEN** 无任何数据的新免费用户打开工作台
- **THEN** 各 section 呈现空态与对应 CTA（上传 CSV/浏览源包/去 AI 构建），不呈现虚构计数

#### Scenario: 深链

- **WHEN** 点击最近数据集条目
- **THEN** 进入该数据集的既有详情页

### Requirement: 工作台落地页候选

`landing_tab` 偏好值域 SHALL 新增 `workbench`：最后停留在工作台的用户裸进
`#/console` 落在工作台；默认落地页不变（「我的数据」）；切页签即写偏好语义
不变。

#### Scenario: 以工作台为落地页

- **WHEN** 用户偏好 landing_tab=workbench 后裸进 `#/console`
- **THEN** 落在工作台页，且该次程序跳转不回写偏好

#### Scenario: 默认落地不变

- **WHEN** 无偏好用户裸进 `#/console`
- **THEN** 仍落在「我的数据」页（既有行为零回归）
