## Purpose

定义柏讯门户的正式登录与身份边界：Logto OIDC（SPA 授权码 + PKCE）验签、Logto sub 与客户账户的映射、内部后台的角色门禁，以及开发占位登录（手填 X-Customer-Id / X-Admin-Token）的退位口径。

## ADDED Requirements

### Requirement: OIDC 登录与令牌卫生

前端以 SPA 授权码 + PKCE 完成 Logto 认证；令牌仅存会话级存储（不落 localStorage、不落服务端日志），支持静默续期与登出。

#### Scenario: 登录获得会话
- **WHEN** 用户经登录流完成认证
- **THEN** 控制台受保护功能可用，且在会话有效期内页面刷新后仍保持登录

#### Scenario: 登出即失效
- **WHEN** 用户执行登出
- **THEN** 会话级令牌清除，受保护功能立即不可用

#### Scenario: 令牌不落持久面
- **WHEN** 检查浏览器持久存储与服务端日志
- **THEN** 不出现访问令牌或 ID 令牌明文

### Requirement: ID token 本地校验

服务端对受保护请求携带的 ID token 做本地校验（JWKS 签名、iss/aud/exp/iat），不因登录链路新增对 IdP 的运行时同步调用；校验失败一律拒绝。

#### Scenario: 伪造令牌被拒
- **WHEN** 携带无效签名、过期或错受众的 ID token 请求受保护端点
- **THEN** 返回 401，且服务端不向 IdP 发起额外网络调用

### Requirement: sub ↔ Customer 映射为唯一客户身份来源

客户侧端点的身份来源是校验后的 Logto sub，经 sub ↔ Customer 映射解析到具体客户；映射缺失的新 sub 走首登自动开户。手填 X-Customer-Id 头不再被客户侧端点接受为身份来源。

#### Scenario: 旧占位头失效
- **WHEN** 仅携带 X-Customer-Id（无有效登录会话）请求客户侧端点
- **THEN** 请求被拒绝

#### Scenario: 映射客户定向
- **WHEN** 已映射的登录客户请求自己的数据集/需求单/用量
- **THEN** 仅作用于其映射客户，跨客户访问被拒

### Requirement: 内部后台角色门禁（双闸）

内部后台在既有 X-Admin-Token 服务端闸之上叠加 Logto `wire-admin` 角色门禁：无该角色的登录用户不可见后台入口且后台 API 拒绝其请求；两道闸同时保留，缺一不可。

#### Scenario: 客户不见后台
- **WHEN** 普通客户身份登录门户
- **THEN** 导航不渲染内部后台入口，直接请求后台 API 亦被拒

#### Scenario: 管理员双闸可用
- **WHEN** 持 wire-admin 角色的登录用户访问后台
- **THEN** 通过角色门禁，且后端 X-Admin-Token 闸照常校验

### Requirement: 登录配置运行时下发

前端登录参数（issuer、client_id 等）经配置端点运行时获取；后端未配置时前端呈现阻塞态提示，不发起假登录流程。

#### Scenario: 未配置阻塞
- **WHEN** 后端 Logto 参数未配置
- **THEN** 控制台显示未配置提示且登录入口不触发认证跳转
