## MODIFIED Requirements

### Requirement: 手动刷新

登录客户可对 kind=dashboard 的产物发起刷新，服务端按契约决策路径：≥1 面板
带合法 query → 契约路径（确定性执行，见「确定性契约刷新」）；0 个 → 无头
AI 轮按各面板 source 重取数并更新 snapshot（既有行为零漂移）。成功产出
新版本（同既有版本化与审计语义）；跨客户 404；archived 不可刷新；同一产物
单飞（409）两条路径共用。

携带 `raw_option` 的面板 MUST 在两条路径下均原样保留（不参与 AI 重取数、
不参与契约查询），响应逐面板结果记为 kept 并标注原因。含 `raw_option`
面板的看板 MUST 不影响其余面板的刷新判定与落盘。

#### Scenario: 刷新即新版本
- **WHEN** 客户对看板点击刷新且 AI 轮成功返回更新后的 spec
- **THEN** 产生新版本号，查看态重渲染呈现新快照，历史版本保留

#### Scenario: 失败零半写
- **WHEN** LLM 输出不合契约或引擎取数失败
- **THEN** 不产生任何新版本，错误如实返回带原因

#### Scenario: 旧看板走 AI 路径
- **WHEN** 看板没有任何面板携带 query 引用
- **THEN** 刷新按既有 AI 无头轮执行（响应 mode 为 ai）

#### Scenario: raw_option 面板不参与 AI 重取
- **WHEN** 看板含 raw_option 面板与其它按 source 取数的面板，且走 AI 路径
- **THEN** raw_option 面板原样保留并记 kept，其余面板照常重取数

### Requirement: 确定性契约刷新

当看板 spec 中 ≥1 面板携带合法 query 引用时，刷新走**契约路径**（零 LLM）：
逐面板按其 query 对本企业数据集执行查询——免费档（api tier）本地执行（零
引擎调用、零计量），cell 档经引擎 query 工具——查询结果按快照契约归一
（columns 投影列序、值归 str/number/null 不丢数据）写入面板 snapshot。
响应携带 `mode: "contract"` 与逐面板结果
（`panels: [{panel_id, status: refreshed|kept|failed, error?}]`）。

回写 MUST 保持面板的 v2 表达力：编码通道、窗格结构、叠加层与面板高度
不因刷新改变，仅数据与快照列序按查询结果更新。`raw_option` 面板 SHALL
按 kept 处理（其数据自含于配置，不经查询契约刷新）。

#### Scenario: 契约刷新即新版本
- **WHEN** 客户对带 query 面板的看板发起刷新且查询成功
- **THEN** 零 LLM 调用产出新版本，契约面板快照更新，面板结构/布局不动

#### Scenario: 面板级失败隔离
- **WHEN** 混合看板中某面板查询失败（数据集不存在/列名非法/引擎拒绝）
- **THEN** 该面板保留原快照并在响应记录原因，其余面板照常刷新，新版本照常落盘

#### Scenario: 全失败零半写
- **WHEN** 看板全部契约面板查询均失败
- **THEN** 不产生任何新版本，502 如实返回各面板失败原因

#### Scenario: 无契约面板保留
- **WHEN** 看板含不带 query 的面板且契约路径触发
- **THEN** 该面板快照原样保留，响应记 kept（不带契约）

#### Scenario: v2 表达力不被刷新抹平
- **WHEN** 客户对含时间轴编码与副图的契约面板发起刷新
- **THEN** 新版本仍保留该编码通道与副图结构，仅数据更新

#### Scenario: raw_option 面板在契约路径保留
- **WHEN** 看板同时含 query 面板与 raw_option 面板并触发契约刷新
- **THEN** query 面板刷新，raw_option 面板原样保留并记 kept
