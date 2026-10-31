# wire-domain-actions Specification

## Purpose
让企业在自己的实体上声明可被舰队消费的业务动作（审批、标记、指派、状态流转），并跑完
propose→confirm→execute→audit 全链。动作类型是客户域一等元数据，执行效果并入既有数据
修正层（以通道归属 `domain-action:*` 区分语义、复用其审计/回滚/重放），确认唯一发生在
柏讯控制台；对外经 wire-ops-mcp 三工具（list/propose/status）供岗位提议，propose 即时返回
pending 不阻塞回合，且提议永不镜像进壹座拍板 lane（单一确认面红线）。

## Requirements

### Requirement: 动作类型声明

动作类型为客户域一等元数据，挂在本企业**实体**上声明：名称（实体内唯一）、风险档
（`write` | `destructive`，v0 枚举）、表单字段 schema（`text|number|date|select` +
`required`；select 仅字面量选项，v0 不做动态选项源）、效果 spec 与启停位。效果 spec v0
MUST 为三种之一：`insert`（value_mapping 将目标数据集列映射到表单字段或字面量）、
`update`（同 insert 外加状态定位——表单字段 MUST 含实体主键列，按主键等值定位目标行）
或 `metadata`（**仅平台内置类型可用**，客户自声明动作效果仍限 insert|update，声明
metadata 效果 422 如实）。平台 SHALL 内置两个声明动作类型：`entity-declare` 与
`entity-relation-declare`——代码定义、非 DB 声明行、挂客户域、不可停用、不可删除、
不可修改、无编辑入口；内置名 SHALL 保留（客户声明同名动作类型 409 如实）；风险档
`write`；字段 schema SHALL 与实体层声明校验器同源（业务名、数据集、主键列、显示列/
注记；源实体列、kind、目标实体主键列、m:n 中间集两列），经列表/提议/状态全通道
呈现如普通域动作，通道零分叉。声明与变更 SHALL append-only 留痕；校验失败 422
字段级且零半写；他企实体 404 不泄露；删除实体 SHALL 级联停用其动作类型（不删历史
实例，内置类型不受任何实体删除影响）；效果引用的数据集列快照按保存时刻校验，漂移
在提议时如实拒绝。

#### Scenario: 声明一个审批动作

- **WHEN** 客户对实体「采购单」声明动作「批准」（risk=write、表单=意见+批准人、
  效果=insert 一行到「审批记录」数据集）
- **THEN** 动作类型落库启用，实体页可见，MCP list 立即可见

#### Scenario: update 效果必须主键定位

- **WHEN** 声明 update 效果但表单字段不含实体主键列
- **THEN** 422 字段级指明「update 效果须含实体主键列以定位目标行」，不落声明行

#### Scenario: 未启用的动作类型不可提议

- **WHEN** 对已停用的动作类型发起提议（控制台或 MCP）
- **THEN** 按「动作未注册」语义拒绝，底层能力未被调用

#### Scenario: 内置声明类型对集群可见

- **WHEN** 舰队岗位经 `wire_action_list` 查本企业动作类型
- **THEN** 返回含 `entity-declare` / `entity-relation-declare`（标注内置），字段
    schema 与实体声明校验器同源，可直接据以构造提议

#### Scenario: 内置名保留

- **WHEN** 客户声明名为 `entity-declare` 的自域动作类型
- **THEN** 409 如实指明内置名保留，不落声明行

#### Scenario: 客户声明不得携带 metadata 效果

- **WHEN** 客户自声明动作类型时效果 spec 为 `metadata`
- **THEN** 422 如实（metadata 效果仅平台内置类型可用），不落声明行

### Requirement: 效果执行落修正层

动作执行 SHALL 按效果类型分通道：`insert` → corrections insert（整行 values）；
`update` → corrections update（主键等值定位、改指定列）；两类修正记录 SHALL 带通道
归属 `domain-action:{type_id}:{instance_id}`（ADR-0006：业务写并入修正层、以通道归属
区分语义），修正层既有审计、回滚、重放语义 SHALL NOT 改变。`metadata` 效果 SHALL 为
修正层外的第三执行通道：直调实体/关系声明既有 service 路径与校验器（同一唯一性、列
快照、主键集合、级联纪律），落库结果与控制台直接声明完全同构；实体/关系为客户域
元数据而非数据行，MUST NOT 产生修正记录、MUST NOT 进入修正层存储与回滚语义（撤销
声明＝既有删除通道）。任何通道：客户源 MUST NOT 因动作执行被写回（红线）；执行本身
MUST NOT 产生数据读取计量（overlay 写与声明元数据写均不入行数额度）；执行结果 SHALL
如实返回（insert|update 含修正 id；metadata 含结果引用——实体 id 或关系 id；失败含
原因），失败零半写（无修正行落库、无半实体半关系行落库）。

#### Scenario: 批准动作落一行

- **WHEN** 一个 insert 型动作经确认执行成功
- **THEN** 目标数据集落一条 insert 修正（actor=domain-action:*），有效视图立即可见，
    修正历史与回滚入口照常可用

#### Scenario: 主键无命中如实失败

- **WHEN** update 型动作的主键定位值在数据集中零行命中
- **THEN** 执行失败并如实返回原因，不落任何修正行，动作实例标记失败可重试

#### Scenario: 声明提案确认落实体

- **WHEN** 一个 `entity-declare` 内置动作经 owner 确认执行成功
- **THEN** 实体与控制台直接声明同构落库（图谱/AI 注入/对象面立即可见），不产生
    任何修正记录，实例台账带结果引用（实体 id）

#### Scenario: 漂移复校零半写

- **WHEN** 声明提议创建后、确认执行前，绑定数据集列快照漂移（主键列被同步删除）
- **THEN** 执行如实失败并在实例台账记录原因，零半写（不落实体行）

#### Scenario: 执行不计量

- **WHEN** 动作执行完成（任何效果类型）
- **THEN** 客户账单与行数额度不因该次执行产生任何读取计量

### Requirement: 确认档位与异步审批

域动作 SHALL 复用动作闸的确认档位语义：客户声明的风险档决定档位（`write` 行内确认；
`destructive` 二次确认并展示影响面摘要）；任何档位 MUST NOT 在客户未确认时执行。
域动作 v0 MUST NOT 允许 `publish` / `cost` 档（对外暴露与计费动作不在域动作语义内，
声明时 422）。异步审批（如未来扩档）沿闸既有「限本队 owner/admin、24h 失效、失败
关闭」语义。确认卡 SHALL 展示按参数生成的影响面（目标数据集、将写入的行内容摘要）。

#### Scenario: write 档行内确认

- **WHEN** agent 或成员提议一个 write 档域动作
- **THEN** 确认卡展示目标数据集与将写入的内容摘要，确认后才执行

#### Scenario: 不存在的档位拒绝

- **WHEN** 客户声明 risk=publish 的域动作
- **THEN** 422 指明域动作 v0 档位枚举，不落声明行

### Requirement: 审计台账

动作实例 SHALL append-only 留痕：动作类型、提议者身份（console 成员 sub 或 MCP
调用身份）、提议来源（控制台 / MCP）、可选来源标注（`source_ref` 原样透传，
≤256 字符——壹座身份行 `fleet=`/`post=`/`title=`/`target=`，`turn=` 字段位
预留）、参数摘要、确认人、执行结果（insert|update 成功含修正 id；metadata 成功含
结果引用——实体 id 或关系 id；失败含原因）、时间戳。
实例台账 MUST 客户域隔离（他企不可见、404 不泄露）、
MUST NOT 提供改写入口；控制台 SHALL 提供按动作类型与时间筛选的台账视图。

#### Scenario: agent 提议的实例可追溯

- **WHEN** 舰队岗位经 MCP 提议某域动作并被人确认执行
- **THEN** 台账实例完整记录提议者（MCP 身份）、来源=mcp、确认人 sub 与执行结果

#### Scenario: 台账只读

- **WHEN** 尝试改写或删除任一动作实例
- **THEN** 无此端点形态（404/405 如实），append-only 保持

#### Scenario: 声明提案留痕身份到岗位

- **WHEN** 本体建设舰队 deployer 岗经 MCP 提议 `entity-declare` 并附来源标注
- **THEN** 台账实例记录 MCP 身份、source_ref 岗位标注与后续确认/执行结果，
    声明落库可追溯至岗位

### Requirement: MCP 提议通道

wire-ops-mcp SHALL 新增三个客户面工具：`wire_action_list`（本企业已启用动作类型
含表单 schema）、`wire_action_propose`（action_type_id + 表单值 + 可选
`source_ref` → 立即返回提议标识与 pending 状态，不阻塞等待确认）、
`wire_action_status`（按实例查询状态与结果）。`source_ref` SHALL 为调用方
原样透传的来源标注字符串（≤256 字符，存台账不解析）——舰队技能稿按壹座在产
身份行 key 名拼装（`fleet=`/`post=`/`title=`/`target=`，`turn=` 字段位预留，
格式演进不影响本接口）。`propose` 的即时性 SHALL 视为与壹座侧回合收尾的
**对偶义务**：实现 MUST NOT 改为阻塞等待人工确认（阻塞将被上游回合如实归因
stalled）。执行端点 MUST NOT 作为任何 MCP 工具暴露（结构性保证：工具面只有
propose，无 execute）。进站 Bearer 原样转发；viewer 角色对 `wire_action_propose`
MUST 拒绝；跨企业 action_type_id 一律 404 不泄露。工具命名沿 verb_noun，
`tool-surface.json` 同批更新（含 `tool_risk` 标签：list/status=`read`、
propose=`write`——只复用既有 risk 值，与壹座 risk→tier 映射逐字节对齐）。
域动作提议与实例 MUST NOT 推送或镜像至壹座拍板面（decision lane）——
唯一确认面在柏讯控制台（paas 评审 R2 共同护栏）。

#### Scenario: 提议立即返回待确认

- **WHEN** agent 调 `wire_action_propose` 提交合法表单值
- **THEN** 返回实例 id 与 pending，不等待人工确认（对偶义务：阻塞实现将被
    壹座回合归因 stalled）；确认后 `wire_action_status` 可查到结果

#### Scenario: 岗位来源标注

- **WHEN** 舰队岗位经 MCP 携带 `source_ref`（壹座身份行原样）提议域动作
- **THEN** 台账实例原样留档该标注（含 `fleet=`/`post=`/`target=` 及未来的
    `turn=` 字段，不解析不校验格式），审计可读到岗位归属

#### Scenario: 工具面无执行通道

- **WHEN** 检查 wire-ops-mcp 工具面
- **THEN** 不存在任何直接执行域动作的工具，执行仅经控制台确认链路

#### Scenario: viewer 不可提议

- **WHEN** viewer 角色成员调用 `wire_action_propose`
- **THEN** 403 如实，未产生提议实例

### Requirement: 控制台管理面与文档

控制台实体页 SHALL 新增「动作」节：动作类型列表（名称/风险徽标/启停/效果摘要）、
声明与编辑表单（字段 schema 构建器 + 效果映射）、实例台账（筛选/状态/结果）。
确认卡 SHALL 复用副驾动作卡组件；i18n zh/en 同步。web 文档页 SHALL 新增「动作」
双语章节（声明→提议→确认→台账的用法与 v0 效果边界如实说明），随代码同批上线
（能力上线同批配文档红线）；公开面（未登录）不暴露任何动作类型与实例。

#### Scenario: 声明全流程

- **WHEN** 客户在实体页声明动作并启用
- **THEN** 动作入列、可被 MCP list 到、确认后台账出现实例

#### Scenario: 文档同批

- **WHEN** 本能力上产
- **THEN** 文档页含「动作」章节（zh/en），说明 v0 效果边界（insert/update、不写回客户源）

#### Scenario: 公开面不外泄

- **WHEN** 未登录访问任何动作类型或实例端点
- **THEN** 401（console 身份门兜底）
