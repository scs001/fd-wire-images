# artifact-lifecycle Delta（wire-rich-text-docs）

## MODIFIED Requirements

### Requirement: 产物持久化

产物MUST为客户域内的一等实体：跨会话持久保存（kind、标题、spec、状态、
来源会话指针、更新时间）；客户只能访问自己的产物；会话草稿可一键固化为
draft 产物且保留来源指针，会话草稿读取端点行为不变；**空白新建**——
console 会话可直接创建空白 dashboard、form、record-view 或 doc draft 产物
（v1=最小空 spec；form 见 form-apps 能力、record-view 见 record-view 能力、
doc 见 rich-text-docs 能力），kind 白名单 v0 为 dashboard、form、record-view
与 doc，title 必填。

#### Scenario: 草稿转正
- **WHEN** 客户把某会话草稿导入为产物
- **THEN** 生成 draft 产物并可跨会话访问，来源会话指针如实记录

#### Scenario: 客户域隔离
- **WHEN** 客户 A 请求客户 B 的产物
- **THEN** 返回 404，不泄露存在性

#### Scenario: 空白建板
- **WHEN** 免费档客户（无 cell）新建看板并起标题
- **THEN** 生成 draft 产物（空面板 spec），可直接进编辑器加面板/绑定数据

#### Scenario: 空白建表单
- **WHEN** 免费档客户（无 cell）新建表单并起标题
- **THEN** 生成 draft 产物（零字段、未绑数据集的最小 form spec），可直接进
    构建器绑数据集配字段

#### Scenario: 空白建溯源页
- **WHEN** 免费档客户（无 cell）新建单记录公开页并起标题
- **THEN** 生成 draft 产物（未绑数据集的最小 record-view spec），可直接进
    构建器绑主数据集配头部与时间轴

#### Scenario: 空白建文档
- **WHEN** 客户新建文档并起标题
- **THEN** 生成 draft 产物（空正文的最小 doc spec），可直接进分栏编辑器写作

#### Scenario: kind 白名单
- **WHEN** 请求以空白方式创建 dashboard、form、record-view 与 doc 之外的 kind
- **THEN** 422 如实拒绝（其他 kind 仍由 AI 草稿转正产生）

## ADDED Requirements

### Requirement: 草稿层与版本链分离

带正文的产物（v1 = doc，见 rich-text-docs 能力）MUST支持**草稿层**（自动保存）
与**版本链**（显式保存）双层模型：自动保存MUST覆盖写草稿层且不产生版本行；
显式保存MUST把草稿层内容追加为不可变版本；草稿层 SHALL NOT 参与状态机判定、
marketplace 可见性判定与分享钉版；版本链 append-only 语义与既有产物逐字相同。
草稿层 SHALL 按产物一对一存续（产物删除时一并清除）。

#### Scenario: 草稿层与版本链分离
- **WHEN** 客户在编辑器里连续自动保存多次后才点一次「保存版本」
- **THEN** 版本链只新增一个版本；草稿层始终持有最新正文；刷新页面后未保存
  的正文不丢失

#### Scenario: 草稿层不泄漏
- **WHEN** 产物处于 draft 状态且从未显式保存过版本
- **THEN** 其他会话/入口读到的正文是草稿层最新内容，且不存在任何版本行
