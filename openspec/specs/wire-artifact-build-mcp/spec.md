# wire-artifact-build-mcp Specification

## Purpose
定义柏讯产物构建 MCP 面在 dashboard（看板）与 doc（正文文档）两类产物上的行为契约：草稿优先、发布独立、零半写、错误带事实、产物带生产来源——与既有 form/record-view 工具同一纪律，使 AI agent 能把分析结论沉淀为柏讯里看得见、可分享的产物。

## Requirements

### Requirement: dashboard 与 doc 的写入工具

产物构建 MCP 面 SHALL 提供 `wire_artifact_dashboard_create` 与 `wire_artifact_doc_create` 两个工具，按 spec 创建对应 kind 的产物。两者 SHALL 复用上游既有 kind 分派校验（dashboard 走 v1 面板结构，doc 走 markdown 正文信封），SHALL NOT 另立第二套结构校验。

#### Scenario: 建看板草稿

- **WHEN** 调用 `wire_artifact_dashboard_create` 传入标题与合法 dashboard v1 spec
- **THEN** 生成一个 draft 看板产物，v1 版本内容等于给定 spec

#### Scenario: 建文档草稿

- **WHEN** 调用 `wire_artifact_doc_create` 传入标题与 `{format:"markdown", text}` 信封
- **THEN** 生成一个 draft doc 产物，正文为给定 markdown

#### Scenario: 结构非法被拒

- **WHEN** dashboard spec 缺 panels，或 doc 信封 format 非 markdown
- **THEN** 422 带原因拒绝，不落任何产物或版本行

### Requirement: 草稿优先，发布独立

创建与更新工具 SHALL 只把产物带到 draft 状态；把产物变为 published SHALL 且仅 SHALL 由独立发布工具完成。创建工具 MUST NOT 接受任何「直接发布」入参或开关，未声明参数 SHALL 由 MCP 层拒绝。

#### Scenario: 创建即发布被拒

- **WHEN** 调用方在创建工具里传 `publish: true`
- **THEN** 调用被 MCP 层拒绝，不触上游

#### Scenario: 发布是独立动作

- **WHEN** 调用方对 draft 产物调用发布工具
- **THEN** 产物转为 published，与创建是两次独立调用

### Requirement: 零半写补偿

由于上游客户面无「一步落完整 spec v1」路径，工具 SHALL 以「空白建档 + spec 写入」两步实现；第二步失败时 SHALL 归档第一步产生的空白产物作为补偿，使失败后不留下可见的半成品。

#### Scenario: 第二步失败补偿

- **WHEN** 空白建档成功而 spec 写入被上游拒绝
- **THEN** 该空白产物被归档，调用返回原样错误，库中无 draft 半成品

### Requirement: 错误口径带事实

spec 引用数据集中不存在的列时，错误响应 SHALL 带上该数据集的真实列名清单；跨企业访问 SHALL 返回与「不存在」不可区分的 404。工具 SHALL NOT 编造列名或示例行。

#### Scenario: 列漂移前移到生成侧

- **WHEN** dashboard spec 绑定了一个不存在的列
- **THEN** 错误文案包含该数据集的真实列名清单，调用方据此修正

#### Scenario: 跨企业不可区分

- **WHEN** 调用方读取或写入不属于自己企业的产物/数据集
- **THEN** 返回 404，与目标不存在不可区分

### Requirement: 产物携带生产来源

经 MCP 创建的产物 SHALL 记录生产来源指针（哪个部署、哪个舰队岗位、哪个回合），与客户手工创建或控制台 AI 创建的产物可区分。该指针 SHALL 随产物版本留痕，SHALL NOT 影响产物本身的 kind 与 spec。

#### Scenario: 产物带来源

- **WHEN** 舰队岗位经 MCP 创建一个看板
- **THEN** 该产物的版本留痕中带有来源指针（部署标识、岗位标识、回合标识）

#### Scenario: 手工创建不受影响

- **WHEN** 客户在控制台手工创建同一 kind 产物
- **THEN** 来源指针为空或标为人工，其余行为不变
