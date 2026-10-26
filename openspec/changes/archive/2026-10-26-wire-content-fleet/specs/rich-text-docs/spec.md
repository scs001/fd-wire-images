## ADDED Requirements

### Requirement: 文档正文 SHALL 支持 agent 经 MCP 写入

doc 正文 SHALL 可由 AI agent 经产物构建 MCP 写入，正文契约（`{format:"markdown", text}`、256 KB 上限、零半写）SHALL 与控制台书写路径完全一致。agent 写入的文档 SHALL 记录生产来源指针；SHALL NOT 引入第二套正文格式或预渲染 HTML 存储。

#### Scenario: agent 写入正文

- **WHEN** 舰队岗位经 MCP 创建或更新一个 doc 产物
- **THEN** 正文按 markdown 契约落库，版本链与控制台书写路径一致

#### Scenario: 超限与格式错误同规

- **WHEN** agent 提交的正文超过体积上限或 format 非 markdown
- **THEN** 422 如实拒绝且零半写，与控制台路径行为一致

#### Scenario: 来源可区分

- **WHEN** 读取一个由 agent 写入的 doc
- **THEN** 可读出其生产来源指针，与人工书写的文档可区分
