# demand-orchestration delta（wire-demand-generation-orchestration）

## ADDED Requirements

### Requirement: 生成工单 SHALL 确定性映射

操作员对 in_progress 需求单触发生成工单时，系统MUST按固定映射产出
health-loop 生成工单 YAML（kind=generate、unit 按源域名派生且仓内已存在
即拒、brief 携带 source_urls/expectations/cadence/notes、golden 声明即
强制、verify 挂标准验证命令），并把工单全文与哈希 append-only 留档于
pipeline_events（workorder_exported）——同一需求单可重出新工单，历史不
改写。工单内容MUST NOT包含客户联系邮箱；公司以脱敏代号留档（公开仓
红线）。

#### Scenario: 出单
- **WHEN** 操作员对带合法源地址的 in_progress 需求单点击「转生成工单」
- **THEN** 产出符合 health-loop 契约的 YAML 工单并留档，需求单状态不变

#### Scenario: slug 冲突如实拒
- **WHEN** 派生的 unit slug 在目标仓已存在
- **THEN** 出单被拒并提示转人工（非 greenfield 不走生成流）

#### Scenario: 脱敏红线
- **WHEN** 工单生成完成
- **THEN** 工单文本不含客户邮箱；公司名以脱敏代号呈现

### Requirement: 工单投递 SHALL 凭据门控且诚实降级

工单投递MUST经舰队拉取器通道：wire 暴露内部拉取端点（内部令牌鉴权）供
fd-industry-data 工作流拉取待投递工单、落仓自推、并回 ack；wire 侧MUST
NOT持有任何外部仓 git 凭据。拉取器未部署时控制台MUST展示工单全文供
人工推送并明示「等待舰队拉取器/人工推送」——MUST NOT在未真实投递时展示
任何自动化成功假象。ack 幂等（同工单重复 ack 不重复记事件）。

#### Scenario: 拉取投递
- **WHEN** 舰队拉取器凭内部令牌拉取待投递工单并回 ack
- **THEN** 工单文件由对方仓工作流落 tickets 路径，wire 侧 pipeline_run 事件留档含可追溯路径

#### Scenario: 诚实降级
- **WHEN** 拉取器未部署（无 ack）
- **THEN** 控制台展示工单全文与人工推送指引，无自动投递假成功文案

#### Scenario: ack 幂等
- **WHEN** 同一工单被重复 ack
- **THEN** 不重复追加事件，返回既有已 ack 状态

#### Scenario: 内部端点鉴权
- **WHEN** 无内部令牌访问拉取或 ack 端点
- **THEN** 401，与既有内部端点口径一致

### Requirement: 状态回流 SHALL 只写真实进度

工单状态MUST经舰队推送回流：拉取器从 tracked 清单取在途工单路径、读
其本地 checkout 的工单文件全文、回推 wire（内部端点），wire 解析映射为
pipeline_events 追加事件与客户可见时间线条目（已投生成/待人工审核/
审核通过点亮/受阻跟进），PR 链接留档；回推幂等（重复推送零追加）。
wire 侧MUST NOT在无真实来源时写入任何进度条目；YAML 不可解析MUST 400
如实回绝。轮询式回流（wire 主动拉公开 raw）仅作出站可达环境下的兜底，
MUST可用环境变量关闭（fd-prod 海外出站被挡，生产默认关）。

#### Scenario: PR 开启回流
- **WHEN** 拉取器回推的工单文件含 pr_url 非空的生命周期条目
- **THEN** pipeline_events 记 pr_open 事件（含 PR 链接），客户时间线出现待审核条目

#### Scenario: 回推幂等
- **WHEN** 同一工单文件被重复回推且状态未变
- **THEN** 零追加事件（幂等去重）

#### Scenario: 坏载荷如实回绝
- **WHEN** 回推内容不是可解析的工单 YAML
- **THEN** 400 回绝并带原因，不写入任何进度

#### Scenario: 生产零出站
- **WHEN** 部署环境无法访问公开 raw 通道（如 fd-prod 海外出站被挡）
- **THEN** 状态回流不受影响（推送式），轮询兜底经环境变量关闭

### Requirement: 客户进度 SHALL 安全子集可见

需求详情时间线MUST展示编排阶段的安全子集（阶段名+PR 链接+时间），
agent 回执全文、重试细节、内部错误串MUST NOT出现在客户面；终态交付
MUST仍由操作员经既有交付端点完成——编排MUST NOT自动置 delivered。

#### Scenario: 安全子集
- **WHEN** 客户查看处于生成流水线中的需求单详情
- **THEN** 时间线呈现阶段化进度与 PR 链接，无任何内部细节字段

#### Scenario: 编排不自动交付
- **WHEN** 工单达到合并点亮终态
- **THEN** 需求单仍为 in_progress，交付由操作员既有端点执行
