## Purpose

定义需求单的轻量收集与三态流转契约：客户以一句话意向单进入，内部以 submitted→in_progress→delivered（+rejected）三态跟踪，为将来"客户对话直接生成爬虫"的自动化闭环预留观测壳。废除非现场实际的八态人肉报价模型。

## ADDED Requirements

### Requirement: 意向单表单 SHALL 轻量三项

客户面需求提交表单MUST仅收三项：需求描述（自由文本，必填，不结构化——按将来对话第一条消息的形态设计）、联系邮箱（必填）、公司名（选填）；从指标目录详情页发起时自动携带指标锚点预填描述。

#### Scenario: 必填校验
- **WHEN** 客户不填需求描述或邮箱提交表单
- **THEN** 提交被拒并逐字段提示，不产生需求单

#### Scenario: 目录锚点预填
- **WHEN** 客户从某指标目录详情页点击"提交数据需求"
- **THEN** 表单描述区预填该指标标识，客户可继续补充后提交

### Requirement: 需求单 SHALL 三态流转

需求单状态机MUST为 submitted → in_progress → delivered，另设 rejected（必带理由）终态；evaluating/quoted/awaiting_confirm/building/delivery_acceptance 五个旧态MUST废除——MUST NOT迁入、不在客户面展示。报价与确认不进状态机（走定价页自助或线下沟通）。

#### Scenario: 合法迁移
- **WHEN** 内部派发台对 submitted 单执行迁移
- **THEN** 仅 in_progress 与 rejected 可选；in_progress 仅可迁 delivered

#### Scenario: 旧态非法
- **WHEN** 任何通道尝试迁入五个废除态
- **THEN** 迁移被拒（服务端为唯一裁决面），旧态名不再出现在客户面任何文案

### Requirement: 提交后 SHALL 展示确认与响应承诺

提交成功后客户MUST看到确认态：需求已收到，承诺 1 个工作日内响应。

#### Scenario: 提交确认文案
- **WHEN** 客户成功提交意向单
- **THEN** 页面展示"已收到，我们会在 1 个工作日内响应"语义的确认信息

### Requirement: 新单 SHALL 邮件通知运营邮箱

新需求单进入 submitted 时，系统MUST向 wire@finddatatech.cloud 发送通知邮件；通知失败MUST NOT阻塞客户提交（提交结果与通知解耦）。

#### Scenario: 通知送达
- **WHEN** 客户成功提交一张新需求单
- **THEN** wire@finddatatech.cloud 收到含需求摘要与联系方式的提醒邮件

#### Scenario: 通知失败不阻塞
- **WHEN** 邮件服务不可用
- **THEN** 客户提交仍成功，通知失败留痕可查，不向客户报错

### Requirement: 我的需求 SHALL 只读列示

控制台"我的需求"页MUST展示本人提交的需求单只读列表：状态徽章（已收到/处理中/已交付，拒绝单展示拒绝理由）、提交时间、需求摘要；不暴露内部操作与状态机细节；跨客户隔离（仅见映射客户自己的单）。

#### Scenario: 列表展示
- **WHEN** 客户进入控制台"我的需求"页
- **THEN** 可见自己全部需求单的状态徽章、提交时间与摘要，无任何可操作迁移的控件

#### Scenario: 跨客户隔离
- **WHEN** 已映射登录客户请求需求列表
- **THEN** 仅返回其映射客户名下的需求单，他企数据不可见

### Requirement: 定价页自定义卡 SHALL 指向需求单

定价页"自定义"询价卡的主 CTA MUST跳转需求单表单；mailto 联系方式MUST降为次级链接保留。

#### Scenario: 主 CTA 行为
- **WHEN** 客户在定价页点击"自定义"卡主按钮
- **THEN** 进入需求单表单页（站内路由），不唤起邮件客户端

### Requirement: 存量 SHALL 一次性归并留痕

历史需求单中处于五个废除态的单子MUST一次性归并为 in_progress；delivered/rejected 存量MUST保持不动；迁移MUST在事件轨迹中留痕且幂等（重跑不产生二次变更）。

#### Scenario: 粗归并
- **WHEN** 迁移执行时存在 quoted/awaiting_confirm/building/delivery_acceptance/evaluating 状态的存量单
- **THEN** 全部变为 in_progress，事件轨迹记录本次归并

#### Scenario: 幂等重跑
- **WHEN** 迁移脚本重复执行
- **THEN** 已归并的单子不再产生新的事件记录
