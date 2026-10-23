## Purpose

定义控制台"AI 构建"入口的过渡形态：客户面的对话 UI 由壹座试用引导卡承接（对话能力本体在壹座），后端对话服务保留为内部能力并为将来客户直连生成（萬星闭环）预留骨架。含 token 网关可达性修复的行为契约。

## ADDED Requirements

### Requirement: AI 构建页 SHALL 呈现壹座试用引导

控制台侧栏的"AI 构建"页MUST展示壹座试用引导卡：主入口指向官网 demo 页（含公示测试账号与导览），次级入口指向壹座正式入口 platform.finddatatech.cloud；卡片上不复述测试账号明文（账号信息以 demo 页为唯一出处）。

#### Scenario: 主入口去 demo 页
- **WHEN** 客户点击引导卡主按钮
- **THEN** 打开官网 demo 页（finddatatech.cloud 对应 demo 路径），该页含公示测试账号与试用导览

#### Scenario: 次级入口直达壹座
- **WHEN** 已有壹座账号的客户点击次级链接
- **THEN** 直达 platform.finddatatech.cloud 登录进入壹座

#### Scenario: 卡片不复述账号
- **WHEN** 引导卡渲染
- **THEN** 卡片文案不含测试账号的邮箱或口令明文，仅引导至 demo 页查看

### Requirement: 控制台 SHALL 默认落地"我的数据"

进入 #/console 未指定子页时，MUST默认落在"我的数据"页而非对话页。

#### Scenario: 默认 tab 切换
- **WHEN** 客户登录后进入 #/console（无子路径）
- **THEN** 直接呈现"我的数据"页内容

### Requirement: 对话后端 SHALL 保留且网关可达

后端对话服务（/api/v1/ai/* 会话、消息、产物端点）MUST保留可用：LLM 调用经 token 网关（token.finddatatech.cloud）可达，不再出现 404；客户面不提供对话 UI，亦不为内部新增对话管理界面（保留是为将来客户直连生成预留骨架）。

#### Scenario: 网关修复后对话可跑通
- **WHEN** 持有效会话凭据直接调用 /api/v1/ai 会话与消息端点发起一轮构建
- **THEN** LLM 调用经 token 网关返回成功，不再出现"token 网关返回 404"类错误

#### Scenario: 客户面无对话界面
- **WHEN** 客户进入控制台任一页
- **THEN** 不存在可输入消息的对话窗口；"AI 构建"页内容为壹座试用引导卡
