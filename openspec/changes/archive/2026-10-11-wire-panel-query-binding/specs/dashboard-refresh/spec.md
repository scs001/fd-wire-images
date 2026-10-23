# dashboard-refresh 变更（wire-panel-query-binding）

## ADDED Requirements

### Requirement: 契约试取端点

`POST /api/v1/artifacts/query-preview`（console 会话身份）：入参与面板 query
同形（dataset_id/filters/sort/limit/columns），经同一解析（非法 422 如实）与
**同一执行路径**（契约刷新的 _query_dataset/_build_snapshot——免费档本地、
cell 档引擎）返回 `{snapshot:{columns,rows}, query:归一后}`；跨客户数据集
404；查询失败如实带原因（引擎拒绝/列不存在）。试取与刷新同代码路径，试取
所见即刷新所得。

#### Scenario: 试取即刷新口径
- **WHEN** 客户对某绑定条件试取随后保存并触发契约刷新
- **THEN** 两次结果来自同一执行路径，快照口径一致

#### Scenario: 非法契约如实拒
- **WHEN** 试取请求携带白名单外算子或空 dataset_id
- **THEN** 返回 422 与原因，不发起任何引擎调用
