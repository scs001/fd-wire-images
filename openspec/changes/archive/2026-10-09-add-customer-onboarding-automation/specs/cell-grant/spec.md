# cell 授权（cell grant）

## Purpose

定义 cell 在 registry 侧授权束的铸造、跨客户隔离不变量与退租回收——客户经 wire 入口只能调用与看见自己的 cell 和共享业务面，授权随开户全自动生效、随退租全自动回收。

## ADDED Requirements

### Requirement: 授权束随开户自动铸造

开户流程完成一户 provision 后，该客户的长命 key 即可经 wire 入口调用自己的 cell 与共享业务面，全程不需要任何 registry 侧手工改动（不手改 scope 文档、不手改组、不重启任何服务）。

#### Scenario: 新客户首调即通
- **WHEN** 开户流程对客户 C 完成 cell provision 与凭据铸造，随后 C 的长命 key 首次经 wire 入口对自己的 cell 发起 initialize
- **THEN** 请求被放行（200），无需任何人工补配置

### Requirement: 跨客户隔离

一个客户的凭据不能调用、也不能经目录发现其他任何客户的 cell。

#### Scenario: 调用别家 cell 被拒
- **WHEN** 客户 A 的长命 key 经 wire 入口请求客户 B 的 cell（initialize 或任何方法）
- **THEN** 请求被拒绝，A 得不到 B cell 的任何工具或数据

#### Scenario: 目录不可见别家 cell
- **WHEN** 客户 A 的长命 key 列出可用服务目录
- **THEN** 目录只含共享业务面与 A 自己的 cell，不含任何其他客户的 cell 条目

### Requirement: 授权即时生效与回收

授权束的铸造与回收在下一次请求即生效，不依赖服务重载；回收按「吊销凭据先行」的顺序执行且幂等可重跑。

#### Scenario: 铸造后下一请求生效
- **WHEN** 授权束铸造完成的瞬间，客户凭据发起请求
- **THEN** 该请求即按新授权判定，无需等待重载窗口

#### Scenario: 退租即拒
- **WHEN** 退租流程吊销客户长命 key 后，该 key 发起任何请求
- **THEN** 下一次调用即被拒绝；重复执行退租流程不报错、不残留授权面

### Requirement: 平台凭据不受隔离影响

平台服务凭据（admin 语义）对全部服务的可达性不因本能力引入的任何隔离而收窄。

#### Scenario: 平台凭据全量可达
- **WHEN** 平台服务凭据经 wire 入口调用任意已启用服务
- **THEN** 行为与引入隔离前一致
