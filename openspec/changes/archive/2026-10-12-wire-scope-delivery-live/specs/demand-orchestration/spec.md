# demand-orchestration Specification

## ADDED Requirements

### Requirement: 交付 scope 重试端点

平台 SHALL 暴露交付 scope 重试端点（内网令牌与管理台两路同语义）：
`POST .../demands/{id}/scope-retry`——仅对 status=delivered 且
scope_status=pending 的需求单受理（其余 409 如实回显当前状态与 scope 态）；
执行语义复用交付动作的既有重试分支（不迁移状态，重试建私有 scope+逐指标
grant），成功置 scope_status=ok 并留档 delivery_events，失败照旧留档并保持
pending；请求可携带可选 indicators/source_dbs 覆盖（缺省 linked_indicators）。

#### Scenario: pending 补课成功

- **WHEN** 对 delivered+pending 且上游 scope API 可用的单发起重试
- **THEN** scope 建立与授权成功，scope_status=ok、scope_name 落档，delivery_events 追加 scope_created

#### Scenario: 非法状态拒

- **WHEN** 对 in_progress 或 delivered+scope_status=ok 的单发起重试
- **THEN** 409，detail 如实回显当前状态与 scope 状态，零副作用

#### Scenario: 重试再失败如实

- **WHEN** 重试时上游仍不可达或拒（如空指标单空规则被 400）
- **THEN** 响应含失败原因，scope_status 保持 pending，delivery_events 追加 scope_create_failed

### Requirement: 运营面 scope 重试与配置接线

管理台需求单页 SHALL 对 delivered 且 scope_status=pending 的单呈现「重试开
scope」动作（点击调管理端点，结果如实呈现：成功显示 scope 名，失败显示新错
误文案）；生产部署 SHALL 配置 SCOPE_API_BASE（fd-prod 内网 scope REST 面地址）
与 SCOPE_API_TOKEN（secret）——未配置时交付与重试照既有诚实降级（pending
留档），配置后新交付自动开 scope。

#### Scenario: 运营补课

- **WHEN** 运营对 pending 单点击「重试开 scope」且上游可用
- **THEN** 按钮态流转为成功，进度/交付面呈现 scope 名与 ok 态

#### Scenario: 未配置仍诚实

- **WHEN** SCOPE_API_BASE 未配置时交付或重试
- **THEN** scope_status=pending，delivery_events 留档「scope API 未配置」，绝不虚构成功
