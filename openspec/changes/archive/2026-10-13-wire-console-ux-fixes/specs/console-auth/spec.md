## MODIFIED Requirements

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

## ADDED Requirements

### Requirement: 登录落地 SHALL 按角色分流

会话建立后的裸控制台入口（#/console 无子路径）落地 SHALL 按登录角色分流：ID token roles 含 wire-admin 的会话落地内部后台（#/admin），其余落地控制台（沿用既有偏好落地序）；显式子路径直达（如 #/console/usage）MUST NOT 被分流劫持。分流是呈现层路由选择，不改变会话与权限模型；无角色的会话即使被引导至后台路由，仍由后台角色门禁拦截。

#### Scenario: 管理员裸登录落后台
- **WHEN** 持 wire-admin 角色的用户登录后到达裸 #/console
- **THEN** 自动落地 #/admin，且该程序跳转不覆盖其偏好落地页设置

#### Scenario: 普通客户落地不变
- **WHEN** 无 wire-admin 角色的客户登录后到达裸 #/console
- **THEN** 按既有偏好落地序落控制台页签，行为与分流引入前一致

#### Scenario: 显式直达不劫持
- **WHEN** 管理员经书签直达 #/console/usage
- **THEN** 停留在控制台用量页，不被分流改跳后台
