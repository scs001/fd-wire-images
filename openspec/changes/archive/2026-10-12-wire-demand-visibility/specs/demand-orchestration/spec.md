# demand-orchestration Specification（delta）

## ADDED Requirements

### Requirement: 管理台交付 SHALL 同语义自动开通私有 scope

admin 派发台的 delivered 迁移 MUST与内部端点逐字同语义：迁移成功后自动发起
客户私有 scope 交付（复用既有 `maybe_auto_deliver` 钩子），scope 面失败只落
`scope_status=pending` 与留档、绝不打断迁移响应。MUST NOT存在「管理台交付
留 scope_status=None、客户私有数据面永不开通」的通道不对称。

#### Scenario: 管理台交付自动开 scope
- **WHEN** 操作员在 admin 派发台把 in_progress 单迁到 delivered
- **THEN** 迁移成功响应照常返回，且 scope 交付已自动发起（成功=scope_status
  ok，失败=pending 留档）——与内部端点行为一致

#### Scenario: scope 失败不打断
- **WHEN** 自动交付钩子执行中 scope 面不可用
- **THEN** 迁移仍成功（delivered），scope_status=pending，留档可经重试端点补课

### Requirement: 运营进度面 SHALL 呈现编排全事件

admin 派发台 MUST为每张需求单呈现编排进度面（数据源=`GET
/internal/admin/demands` 已返回的 pipeline_events/delivery_events/gate/
scope_status/scope_name，MUST NOT新增聚合端点）：事件时间线按序展示
workorder_exported（已出单，含 ticket_path）、pipeline_run（已投递）、
workorder_submitted（已入生成流水线）、pr_open（待人工审核，含 PR 链接）、
merged（已合并点亮）、blocked（受阻）、workorder_lost（工单丢失）各阶段的
发生时间；gate 摘要（通过/尝试/流水线运行数）；交付状态（scope 就绪/
挂起）。未出单的单 MUST如实占位「尚未出单」，MUST NOT虚构进度。

#### Scenario: 在途单进度可见
- **WHEN** 操作员展开一张已出单且工单在生成流水线中的需求单
- **THEN** 可见已出单→已投递→已入生成各阶段时间与 ticket_path，后续阶段
  如实缺席（未发生即不渲染）

#### Scenario: 未出单占位
- **WHEN** 操作员展开一张从未出单的需求单
- **THEN** 进度面显示「尚未出单」占位，无任何虚构阶段

### Requirement: 客户交付就绪 SHALL 可见且无内部细节

控制台「我的需求」页 MUST在 delivered 单上渲染交付就绪信号：私有数据面
已就绪（scope_status=ok）/准备中（pending）；已授权转公开时呈现公开状态。
非 delivered 单 MUST NOT渲染该区块；delivery_events 全文、失败错误串、
内部字段名 MUST NOT出现在客户面（安全子集纪律同编排时间线）。

#### Scenario: 交付就绪呈现
- **WHEN** 客户查看一张 delivered 且 scope_status=ok 的需求单
- **THEN** 列表该单出现「私有数据面已就绪」语义的交付区块

#### Scenario: 非交付单不渲染
- **WHEN** 客户查看 submitted/in_progress/rejected 单
- **THEN** 无交付就绪区块；页面无任何 delivery_events 内部细节字段
