# demand-intake Specification（delta）

## ADDED Requirements

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
