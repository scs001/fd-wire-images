## Purpose

定义免费档自助注册到可调用的全自动开户：新用户首次登录即触发既有开户 ceremony（Customer 行、wgk- key、sub2api 支付账户、预检映射、registry 授权束），以及免费档边界（有 key 无 cell）与滥用治理路径。

## ADDED Requirements

### Requirement: 首登自动开户

新 Logto sub 首次登录控制台即自动完成开户全套：Customer 行落库、wgk- key 铸造、sub2api 支付账户代建、caller 预检映射写入、registry 授权束下发，全程无人工。链路任一步失败则开户中止，已执行步骤幂等可重试，不留可被放行的半开户态。

#### Scenario: 新用户首登即得 key
- **WHEN** 完成邮箱验证的新用户首次登录控制台
- **THEN** 控制台可见其 wgk- key（明文仅首次展示），且立即能以 200 调用其授权范围

#### Scenario: 中止不留半开户
- **WHEN** 开户链路某步失败
- **THEN** 重试从断点幂等续作，不存在"有账户无 key""有 key 无映射"等中间态被调用放行

### Requirement: 免费档边界

免费注册默认免费档：获得 wgk- key 与 1,000 行/月读取额度，不配给 cell；「我的数据」/物化功能不可用并提示升级。cell 自首个付费档（基础档）起配。

#### Scenario: 免费用户无 cell
- **WHEN** 免费档用户打开「我的数据」
- **THEN** 呈现升级引导，且不为该用户创建任何 cell 资源

### Requirement: 滥用防护与治理

注册需邮箱验证，每身份限一户；治理手段为 suspend（停调用）、key 吊销、退租，全部复用既有幂等通道。免费档注册不产生 cell 容器成本。

#### Scenario: 停用即断
- **WHEN** admin 将滥用客户置为 suspended
- **THEN** 该客户后续经入口的调用被拒，已铸 key 同步失效

### Requirement: 升档扩权

免费客户升档至付费档后，cell 配给与月度额度按新档即时生效；在线自助充值通道不在本能力范围（首期升档经联系开通/内部操作），降档与退租沿用既有幂等语义。

#### Scenario: 升档得 cell
- **WHEN** 免费客户被升至基础档（¥10）
- **THEN** cell 配给完成，月度额度按基础档含额生效
