## MODIFIED Requirements

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
