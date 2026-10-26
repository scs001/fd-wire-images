## Why

柏讯三段生产能力（爬虫构建、看板/文本产物创建、内容持续迭代）零件都在，但没有一条把它们编成可安装、可自我迭代的形态。三个实测缺口：22 个已发布套件**全部只声明只读数据服务**（`wire-build-mcp`/`wire-ops-mcp` 一次都没被声明）；22 个 pack manifest **零个带 `fleet` 段**；产物创建（dashboard/doc）只活在控制台自己的 AI 循环里（`ai_draft_artifacts`），没有 MCP 面。结果是柏讯能给结论、不能给产物；能人工运营、不能自迭代。

同时，萬星的舰队机制（pack 内嵌 `fleet` 段 → 岗位/节奏/KPI/报数）与 `existing-a2a` 目标位已经端到端存在，爬虫自愈链已用它跑通（`fd-industry-data` ADR-0001）。本变更不是新建编排层，是把柏讯的三段能力接到这条既有轨道上，并**同一份 pack 同时服务内部狗粮与外部开放**。

## What Changes

- **产物构建 MCP 补齐**：`wire-build-mcp` 新增 `wire_artifact_dashboard_create`、`wire_artifact_doc_create` 两个工具（草稿优先、发布独立，与既有 form/record-view 同纪律）。上游 `fd_wire_api/ai/tools.py` 的 `dashboard_spec_create`/`research_report_create` 与 `validate_kind_spec` 的 kind 分派已存在，本变更把它们开成 MCP 面。
- **两篇技能稿**：`wire-artifact-build`、`wire-data-steward`（名字已在 `wire-build-mcp/tool-surface.json` 的 `skills_eligible_for_packs` 声明，稿未写）。
- **两个生产型 pack**：「柏讯·产物构建」「柏讯·数据治理」，各自声明 `mcpServers: [wire-build-mcp]`。
- **分析套件升级**：22 个既有套件 manifest 追加 `wire-build-mcp` 声明，使分析结论可沉淀为产物（可选声明，不改变只读路径）。
- **舰队模板 A / B**：内部狗粮（运营号 cell，平台凭据，节奏由覆盖缺口/质量快照/gate 驱动）与外部开放（客户 cell，客户凭据即物化轨，节奏由需求单/使用/告警驱动）。两者共用同一批 pack，只在模板、凭据、信号上分叉。
- **收敛判据**：周期物化仍归既有 `refresh-scheduler`（已有地板与配额），agent 只判断「内容该不该变」；舰队不得另建与刷新调度重复的定时物化。
- **不做**：新 agent 运行时、新调度器、流式/Kafka、跨线进程内耦合、绕过 `check_presets.py` 与 PR 人工门的产品内容自改写。

## Capabilities

### New Capabilities

- `wire-content-fleet`: 柏讯 pack 的舰队化契约——pack 内嵌 `fleet` 段、内部/外部两种安装上下文、凭据轨与信号驱动的节奏、收敛判据、产品内容自进化的人工门。
- `wire-artifact-build-mcp`: 产物构建 MCP 面的行为契约——dashboard/doc 两个写入工具的入参/草稿语义/错误口径，与既有 form/record-view 工具同一纪律。

### Modified Capabilities

- `artifact-lifecycle`: 产物创建面从「控制台 AI 循环」扩到「MCP 工具」；MCP 创建的产物携带生产来源指针（哪个舰队回合产生），kind 白名单与零半写语义不变。
- `rich-text-docs`: doc 正文可由 agent 经 MCP 写入；Markdown 为正文事实源、体积上限、零半写不变；agent 写入须带来源与证据引用。
- `dashboard-renderer`: dashboard v1 spec 可由 MCP 产出；校验与渲染走与控制台同一条 `validate_kind_spec` 与渲染路径，不新增第二套结构。
- `refresh-scheduler`: 明确周期物化的唯一归属——舰队节奏不得替代或重复刷新调度；新增「舰队产物的刷新归属」判定规则。

## Impact

- **fd-wire/api**：产物带内容创建路径（MCP 可调）与生产来源指针；`validate_kind_spec` 复用不复制。
- **wire-build-mcp**（工作区根包）：新增 2 工具、`tool-surface.json` 工具组与 `tool_risk` 更新。
- **docs/wire-presets**：2 个 pack manifest、2 篇技能稿、22 个既有 manifest 追加声明；`check_presets.py` 与 `pack-server-closure` 校验照跑。
- **paas**：无需改动——`fleet-template.js` 与 `provisionFleetFromTemplate` 已支持 `pack-preset` / `existing-a2a` 目标位。
- **fd-wanxing**：无需改动——A2A 服务面、凭据轨、missions 成本闸已在。
- **工作区根 openspec**：`wire-build-mcp` 与 `wire-analysis-presets` 两份 spec 需在同步阶段补 delta（不在本仓根内，见 tasks 的跨根条目）。
- **红线不变**：公开目录不含客户数据、分享页不被检索命中、代爬合规门双闸、双凭据计量归属、PR 人工门。
