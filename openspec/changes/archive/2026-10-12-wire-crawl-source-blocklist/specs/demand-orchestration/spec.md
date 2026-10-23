# demand-orchestration Specification（delta）

## ADDED Requirements

### Requirement: 出单闸 SHALL 复检禁爬源

工单导出 MUST对需求单存量 source_urls 复检禁爬清单：命中 MUST 409 如实
拒绝并说明命中平台，MUST NOT 产出含禁爬源的工单、MUST NOT 留下
workorder_exported 留档（零半写）。复检与提交口共用同一清单与匹配规则
（单一出处）。

#### Scenario: 存量单带禁爬源出单被拒
- **WHEN** 运营对一张（历史遗留或直接入库的）source_urls 含 pinduoduo.com 的 in_progress 单触发出单
- **THEN** 409，文案点名平台；pipeline_events 零新增

#### Scenario: 合规单出单不受影响
- **WHEN** 运营对 source_urls 全部合规的单触发出单
- **THEN** 行为与既有逐字一致（工单产出+留档+入队）
