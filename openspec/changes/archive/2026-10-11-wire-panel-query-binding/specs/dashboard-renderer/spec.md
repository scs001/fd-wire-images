# dashboard-renderer 变更（wire-panel-query-binding）

## ADDED Requirements

### Requirement: 契约表单化绑定

编辑器面板详情区提供「绑定查询」段（未绑定的面板显示未绑定态）：数据集
选择（本企业数据集列表，呈现 tier 与行数）、投影列勾选（按数据集列序）、
过滤行（列 + 算子白名单 + 值）、排序（列 + asc/desc）、limit（1..1000）；
「试取数据」把试取结果写入面板本地快照（预览即所得，不自动保存）；「解绑」
清除面板 query；kpi 面板给「最近一期 + limit 1 + 投影值列」约定提示；保存
走既有版本链（query 随面板落盘）；已有 query 的面板表单回填原值。

#### Scenario: 人工绑定即契约
- **WHEN** 客户在编辑器选数据集、勾两列、加一条过滤并保存
- **THEN** 新版本面板 query 含该 dataset_id/列投影/过滤，刷新徽标仍呈现

#### Scenario: 试取预览
- **WHEN** 客户填好绑定条件点「试取数据」
- **THEN** 面板本地快照更新为试取结果（预览呈现），未保存离开仍提示

#### Scenario: 解绑
- **WHEN** 客户对已绑定面板点「解绑」并保存
- **THEN** 新版本面板不再携带 query，后续刷新按无契约面板处理
