# console-auth Specification

## Purpose
定义柏讯门户的正式登录与身份边界：Logto OIDC（SPA 授权码 + PKCE）验签、Logto sub 与客户账户的映射、内部后台的角色门禁，以及开发占位登录（手填 X-Customer-Id / X-Admin-Token）的退位口径。

## Requirements

### Requirement: OIDC 登录与令牌卫生

前端以 SPA 授权码 + PKCE 完成 Logto 认证；令牌仅存会话级存储（不落 localStorage、不落服务端日志），支持静默续期与登出。静默续期具备超时兜底（超时按失败处理、清会话回落登录引导，不无限等待）；控制台启动期呈现可见加载态（不渲染空白页）。

#### Scenario: 登录获得会话
- **WHEN** 用户经登录流完成认证
- **THEN** 控制台受保护功能可用，且在会话有效期内页面刷新后仍保持登录

#### Scenario: 登出即失效
- **WHEN** 用户执行登出
- **THEN** 会话级令牌清除，受保护功能立即不可用

#### Scenario: 令牌不落持久面
- **WHEN** 检查浏览器持久存储与服务端日志
- **THEN** 不出现访问令牌或 ID 令牌明文

#### Scenario: 续期挂起有界
- **WHEN** 静默续期在超时窗内未完成（如浏览器沙箱阻断续期 iframe）
- **THEN** 按续期失败处理回落登录引导，控制台不停留在无反馈的空白页

#### Scenario: 启动加载可见
- **WHEN** 控制台处于启动 loading 态（配置拉取/会话建立中）
- **THEN** 页面呈现可见的加载指示而非空白

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

内部后台在既有 X-Admin-Token 服务端闸之上叠加 Logto `wire-admin` 角色门禁：无该角色的登录用户不可见后台入口且后台 API 拒绝其请求；两道闸同时保留，缺一不可。双闸覆盖面 SHALL 包含全部运营只读面——`/internal/admin/*` 端点族与客户本地数据运营概览端点（per-cell 存储用量与连接器健康）同闸放行，后者不再挂内部通道单令牌闸（X-Internal-Token）。

#### Scenario: 客户不见后台
- **WHEN** 普通客户身份登录门户
- **THEN** 导航不渲染内部后台入口，直接请求后台 API 亦被拒

#### Scenario: 管理员双闸可用
- **WHEN** 持 wire-admin 角色的登录用户访问后台
- **THEN** 通过角色门禁，且后端 X-Admin-Token 闸照常校验

#### Scenario: 本地数据概览同闸受控
- **WHEN** 持 wire-admin 角色的运营人员经门户请求客户本地数据概览
- **THEN** 请求经双闸（X-Admin-Token 由网关注入 + Bearer 角色）放行返回概览数据；无 wire-admin 角色的登录身份请求同一端点被 403 拒绝

### Requirement: 登录配置运行时下发

前端登录参数（issuer、client_id 等）经配置端点运行时获取；后端未配置时前端呈现阻塞态提示，不发起假登录流程。

#### Scenario: 未配置阻塞
- **WHEN** 后端 Logto 参数未配置
- **THEN** 控制台显示未配置提示且登录入口不触发认证跳转

### Requirement: 登录落地 SHALL 按角色分流

会话建立后的裸控制台入口（#/console 无子路径）落地 SHALL 按登录角色分流：ID token roles 含 wire-admin 的会话落地内部后台（#/admin），其余落地控制台（沿用既有偏好落地序）；显式子路径直达（如 #/console/usage）MUST NOT 被分流劫持。分流是呈现层路由选择，不改变会话与权限模型；无角色的会话即使被引导至后台路由，仍由后台角色门禁拦截。

分流 SHALL NOT 抹掉管理员的控制台可达性：落后台后管理员 SHALL 保有进入控制台的显式出口，且其作为客户的第一人称资产（看板、产物、我的数据、实体、用量、团队、市场）SHALL 保持可达。门户文档页链向控制台的入口（如快速开始区块「前往控制台」）对管理员会话 SHALL 保持可达，且到达后顶部导航 SHALL 照常呈现，不因落后台而消失。

#### Scenario: 管理员裸登录落后台
- **WHEN** 持 wire-admin 角色的用户登录后到达裸 #/console
- **THEN** 自动落地 #/admin，且该程序跳转不覆盖其偏好落地页设置

#### Scenario: 普通客户落地不变
- **WHEN** 无 wire-admin 角色的客户登录后到达裸 #/console
- **THEN** 按既有偏好落地序落控制台页签，行为与分流引入前一致

#### Scenario: 显式直达不劫持
- **WHEN** 管理员经书签直达 #/console/usage
- **THEN** 停留在控制台用量页，不被分流改跳后台

#### Scenario: 管理员可从后台进控制台
- **WHEN** 持 wire-admin 角色的用户在内部后台任一页面点击控制台出口
- **THEN** 进入控制台，看板与产物等客户资产页可达，与无该角色的客户呈现一致

#### Scenario: 管理员经文档页入口进控制台
- **WHEN** 持 wire-admin 角色的用户在门户文档页点击「前往控制台」
- **THEN** 到达控制台而非被改跳后台，且门户顶部导航照常呈现

#### Scenario: 显式子路径经文档入口不被劫持
- **WHEN** 任一已登录用户经文档页直达 #/console/artifacts 等显式子路径
- **THEN** 停留在该页签，分流不改写其 hash
