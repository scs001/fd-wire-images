# wire-content-fleet Specification

## Purpose
定义柏讯能力以「pack + 舰队」形态交付与运行的契约：同一份 pack 同时服务内部狗粮与外部客户两种安装上下文，凭据轨决定计量归属，节奏由真实信号驱动而非盲跑，周期物化归既有刷新调度，产品内容的自进化止于人工门。

## Requirements

### Requirement: 产物生产型 pack SHALL 声明其构建服务

pack manifest 的 `mcpServers` SHALL 声明该 pack 全部技能点名的服务（沿用 `pack-server-closure` 校验）。凡是技能会写入产物的 pack（产物构建、数据治理、以及任何声明「把结论沉淀为产物」的分析套件），`mcpServers` SHALL 包含产物构建服务；只读分析 pack MAY 不声明，其行为与现状一致。

#### Scenario: 产物生产型 pack 声明构建服务

- **WHEN** 一个 pack 的技能正文引用 `mcp__wire-build-mcp__wire_artifact_dashboard_create`
- **THEN** 该 pack 的 `mcpServers` 含 `wire-build-mcp` 条目，`pack-server-closure` 校验通过

#### Scenario: 只读分析 pack 行为不变

- **WHEN** 一个既有分析套件不声明产物构建服务
- **THEN** 其工具面、安装行为与计量归属逐字节不变

### Requirement: pack MAY 内嵌 fleet 段作为舰队模板

pack manifest MAY 携带 `fleet` 段（沿用壹座 `fleet-template` 契约：members/ref 树/节奏/目标位）。带 `fleet` 段的 pack 安装时 SHALL 实例化一支全暂停的舰队，由安装者显式启动；不带 `fleet` 段的 pack SHALL 行为与现状完全一致。

#### Scenario: 带舰队段的 pack 安装即出舰队

- **WHEN** 安装一个含 `fleet` 段的 pack
- **THEN** 生成一支全暂停舰队，岗位、KPI 槽位与节奏按模板落库，安装者启动后才执行

#### Scenario: 不带舰队段的 pack 行为不变

- **WHEN** 安装一个不含 `fleet` 段的 pack
- **THEN** 不产生任何舰队或岗位，安装路径与现状一致

### Requirement: 同一 pack 支持内部与外部两种安装上下文

pack SHALL NOT 为内部狗粮与外部客户各做一份。两种上下文的差异 SHALL 且仅 SHALL 限于三处：舰队模板（岗位编排与节奏）、凭据轨、驱动信号。产物契约（kind + spec + 校验）SHALL 两侧共用同一条路径。

#### Scenario: 内部安装

- **WHEN** 运营号在自己的 cell 安装 pack
- **THEN** 使用内部舰队模板，产出物为柏讯自营内容，计量记平台侧

#### Scenario: 外部安装

- **WHEN** 客户在其 cell 安装同一 pack
- **THEN** 使用外部舰队模板，产出物落在客户域，计量按客户凭据归属

#### Scenario: 产物契约不因上下文分叉

- **WHEN** 两种上下文分别创建同 kind 产物
- **THEN** 两者走同一个 kind 分派校验与渲染路径，不出现第二套结构

### Requirement: 凭据轨决定计量归属

舰队回合的凭据轨 SHALL 按既有双凭据计量归属判定：探索性读取走平台凭据，产物物化走客户凭据。内部狗粮 SHALL 走平台/主人池；外部 SHALL 走客户自带凭据。平台补贴 SHALL NOT 覆盖无人值守的周期性舰队回合。

#### Scenario: 外部物化记客户账

- **WHEN** 客户 cell 的舰队回合物化一个产物
- **THEN** 该回合计量记入客户凭据轨道，不落平台补贴

#### Scenario: 内部回合不记客户账

- **WHEN** 运营号 cell 的舰队回合运行
- **THEN** 计量记平台侧，不出现在任何客户账单

### Requirement: 舰队节奏 SHALL 由信号驱动

无人值守舰队 SHALL 以真实业务信号作为发起依据（覆盖缺口、质量快照、需求单、使用与告警），SHALL NOT 仅凭固定 cron 无差别轮转。无信号时 SHALL 不发回合，并在报数中如实声明「本轮无信号」。

#### Scenario: 无信号不发回合

- **WHEN** 一个周期内没有新信号
- **THEN** 舰队不发执行回合，岗位报数记录「无信号」

#### Scenario: 信号到达才起回合

- **WHEN** 出现新需求单、覆盖缺口或质量异常
- **THEN** 对应岗位按信号起回合，回合 brief 携带该信号的证据

### Requirement: 周期物化归刷新调度，舰队不得重复

产物的周期性数据物化 SHALL 由既有 `refresh-scheduler` 承担（含间隔地板与客户配额）。舰队 SHALL NOT 为同一产物另建等价的定时物化节奏；舰队回合 SHALL 只判断内容是否应当变更，变更时更新产物 spec，物化由刷新调度或显式调用完成。

#### Scenario: 舰队产出物需要周期刷新

- **WHEN** 舰队创建了一个需要持续更新的产物
- **THEN** 为该产物建立刷新调度，而非给岗位加一条等价 cron

#### Scenario: 重复节奏被拒

- **WHEN** 一个岗位的节奏与某产物的刷新调度等价且目标同一产物
- **THEN** 配置校验拒绝该重复，并指向既有刷新调度

### Requirement: 产品内容自进化 SHALL 止于人工门

agent 对柏讯自有内容（技能稿、套件 manifest、示例产物）的修改 SHALL 以分支 PR 形式提交并附验证证据（`check_presets.py`、`pack-server-closure`、真校验器）。自动路径 SHALL NOT 自行 merge、SHALL NOT 点亮任何调度、SHALL NOT 改动工单目标以外的文件。

#### Scenario: 自进化产出 PR

- **WHEN** 舰队完成一轮技能稿或套件 manifest 修改
- **THEN** 提交一个带验证证据的分支 PR，等待人工 merge

#### Scenario: 越权被拒

- **WHEN** 自动路径尝试直接 merge、点亮调度或改动范围外文件
- **THEN** 该动作被拒绝并留下拒绝原因
