## Purpose

定义分析产物（dashboard/composite_indicator/research_report/alert_rule/
refresh_schedule 五类）的生命周期管理：跨会话持久化、append-only 版本化、
draft/published/archived 状态机、会话草稿导入，以及控制台产物页的服务端接真——
使 AI 生成的分析成果可保存、可迭代、可稳定引用，为看板编辑、分享与物化执行
奠基。

## ADDED Requirements

### Requirement: 产物持久化

产物为客户域内的一等实体：跨会话持久保存（kind、标题、spec、状态、来源会话
指针、更新时间）；客户只能访问自己的产物；会话草稿可一键固化为 draft 产物且
保留来源指针，会话草稿读取端点行为不变。

#### Scenario: 草稿转正
- **WHEN** 客户把某会话草稿导入为产物
- **THEN** 生成 draft 产物并可跨会话访问，来源会话指针如实记录

#### Scenario: 客户域隔离
- **WHEN** 客户 A 请求客户 B 的产物
- **THEN** 返回 404，不泄露存在性

### Requirement: 版本化

产物 spec 的每次保存追加一个不可变版本（append-only）；当前版本为最高版本号；
任意历史版本可列出与查看；archived 产物拒绝 spec 修改（恢复后可改）。

#### Scenario: 保存即新版本
- **WHEN** 客户编辑产物 spec 并保存两次
- **THEN** 版本号递增且两个历史版本均完整可查，历史版本不可被修改

### Requirement: 状态机

产物状态为 draft → published → archived 单向迁移，archived → draft 恢复允许；
每次迁移留操作记录；published 是对外稳定引用位（物化/分享挂接点在本能力之外）。

#### Scenario: 发布与归档
- **WHEN** 客户依次对 draft 产物执行发布、归档
- **THEN** 状态依次迁移且操作留档；归档后 spec 修改被拒并提示恢复路径

#### Scenario: 非法迁移拒绝
- **WHEN** 对 archived 产物直接执行发布
- **THEN** 返回 409 并说明合法迁移路径

### Requirement: 控制台产物页接真

产物页的列表、spec 编辑保存、版本历史、发布/归档操作全部走服务端 API；
localStorage 仅作未保存离开提示的本地缓冲，不再是保存目的地。

#### Scenario: 编辑不丢失
- **WHEN** 客户编辑产物 spec 保存后刷新页面
- **THEN** 重新打开看到已保存的最新版本内容与版本号
