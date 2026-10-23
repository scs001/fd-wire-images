# artifact-lifecycle 变更（wire-dashboard-blank-create）

## MODIFIED Requirements

### Requirement: 产物持久化

产物为客户域内的一等实体：跨会话持久保存（kind、标题、spec、状态、来源会话
指针、更新时间）；客户只能访问自己的产物；会话草稿可一键固化为 draft 产物且
保留来源指针，会话草稿读取端点行为不变；**空白新建**——console 会话可直接
创建空白 dashboard draft 产物（v1=最小空板 spec），kind 白名单 v0 仅
dashboard，title 必填。

#### Scenario: 草稿转正
- **WHEN** 客户把某会话草稿导入为产物
- **THEN** 生成 draft 产物并可跨会话访问，来源会话指针如实记录

#### Scenario: 客户域隔离
- **WHEN** 客户 A 请求客户 B 的产物
- **THEN** 返回 404，不泄露存在性

#### Scenario: 空白建板
- **WHEN** 免费档客户（无 cell）新建看板并起标题
- **THEN** 生成 draft 产物（空面板 spec），可直接进编辑器加面板/绑定数据

#### Scenario: kind 白名单
- **WHEN** 请求以空白方式创建非 dashboard kind
- **THEN** 422 如实拒绝（其他 kind 仍由 AI 草稿转正产生）
