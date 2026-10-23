# demand-intake Specification

## Purpose

定义需求单的轻量收集与三态流转契约：客户以一句话意向单进入，内部以 submitted→in_progress→delivered（+rejected）三态跟踪，为将来"客户对话直接生成爬虫"的自动化闭环预留观测壳。废除非现场实际的八态人肉报价模型。

## Requirements

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

### Requirement: 禁爬源 SHALL 在提交口硬拒

需求单提交的源地址（source_urls）MUST逐条过平台禁爬清单校验；命中禁爬
平台的地址 MUST 422 拒绝并在错误中带平台域名与理由。禁爬清单首版 MUST
含淘宝系（taobao.com/tmall.com/alibaba.com/1688.com/alipay.com）与拼多多
系（pinduoduo.com/yangkeduo.com/pddpic.com/duoduoyoucai.com）。匹配 MUST
为域名后缀级（host 等于清单域名或以 ``.<域名>`` 结尾，大小写不敏感、含
端口与子域），MUST NOT 被 ``<禁爬域名>.<他域>`` 形态绕过。校验与既有合规
门（非 http(s)、内嵌账密）同点生效——任一条非法即整单拒绝。

#### Scenario: 淘宝地址拒
- **WHEN** 客户提交含 https://item.taobao.com/xxx 的意向单
- **THEN** 422，错误定位该条并说明该平台数据不在代爬服务范围

#### Scenario: 拼多多地址拒
- **WHEN** 客户提交含 https://mobile.yangkeduo.com/xxx 的意向单
- **THEN** 422 同口径拒绝

#### Scenario: 子域与后缀伪装
- **WHEN** 提交 https://taobao.com.evil.example/x 或 https://sub.tmall.com/y
- **THEN** 前者放行（非淘宝域），后者拒绝（真子域命中）

#### Scenario: 正常公开源不受影响
- **WHEN** 提交 https://www.stats.gov.cn/ 等非禁爬平台地址
- **THEN** 校验通过，正常受理
