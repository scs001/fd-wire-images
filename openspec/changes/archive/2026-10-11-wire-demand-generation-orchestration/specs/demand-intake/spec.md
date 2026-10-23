# demand-intake 变更（wire-demand-generation-orchestration）

## REMOVED Requirements

### Requirement: 意向单表单 SHALL 轻量三项

（改名重立为「意向单表单 SHALL 轻量收集」——表单从三项扩展为含源地址与
公开声明，标题如实更新；场景在 ADDED 版携带并扩充。）

## ADDED Requirements

### Requirement: 意向单表单 SHALL 轻量收集

客户面需求提交表单MUST收：需求描述（自由文本，必填，不结构化——按将来
对话第一条消息的形态设计）、联系邮箱（必填）、公司名（选填）、数据源地址
（≥1 个 URL，公开数据需求的结构化入口）、公开数据声明（必勾——客户确认
所求数据为公开可获取数据）；从指标目录详情页发起时自动携带指标锚点预填
描述。服务端MUST硬校验：源地址仅 http/https、不得内嵌账密
（user:pass@host 形态拒绝）、声明缺失即拒。

#### Scenario: 必填校验
- **WHEN** 客户不填需求描述或邮箱提交表单
- **THEN** 提交被拒并逐字段提示，不产生需求单

#### Scenario: 目录锚点预填
- **WHEN** 客户从某指标目录详情页点击"提交数据需求"
- **THEN** 表单描述区预填该指标标识，客户可继续补充后提交

#### Scenario: 源地址形态硬校验
- **WHEN** 客户提交的数据源地址不是 http(s) 或内嵌账密
- **THEN** 提交被拒并逐条指明非法地址，不产生需求单

#### Scenario: 公开数据声明门
- **WHEN** 客户未勾选公开数据声明提交表单
- **THEN** 提交被拒（非公开数据不在代爬服务面，提示线下沟通），不产生需求单
