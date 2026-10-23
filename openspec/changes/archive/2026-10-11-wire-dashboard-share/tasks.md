# Tasks — wire-dashboard-share

## 1. 签名与端点（api）

- [x] 1.1 share 模块：FD_WIRE_SHARE_SECRET 配置（未配置→两端正点 503 带原因）；HMAC-SHA256 令牌（artifact_id|version|exp，默认 30 天，常数时间比较）；单测：生成/验签/过期/篡改
- [x] 1.2 POST /api/v1/artifacts/{id}/share：仅 published 且 kind=dashboard（其他 403/404 明确）；返回 url+expires_at+version；跨客户 404
- [x] 1.3 GET /api/v1/share/dashboard/{id}?t=：公开无鉴权；验签+版本匹配+published 校验，任一不满足 404（不泄露原因差异）；返回 {title, spec, version, shared_at}
- [x] 1.4 ai/tools.py dashboard_spec_create 描述内嵌 v1 结构示例（panels/type/span/snapshot 形状）；相关测试快照更新

## 2. web（web）

- [x] 2.1 ShareView（公开路由 /#/share/dashboard/:id?t=）：免登录、复用 DashboardRenderer 只读、页脚「由柏讯分享」+版本号、noindex meta、标题「分享 · 」前缀；令牌无效/过期呈现「链接无效或已过期」页（无重试泄露）
- [x] 2.2 ArtifactsPage：published dashboard 卡片「生成分享链接」（弹层展示 url+到期+复制按钮）；draft/archived/非 dashboard 无入口；i18n ZH/EN
- [x] 2.3 组件测试：ShareView 三态（正常渲染/令牌坏/服务端 503 禁用）、入口按 kind+status 分发

## 3. 运维与验证

- [ ] 3.1 生产接线：fd-wire-env secret 增 share_secret（随机 32 字节）+ 清单 env FD_WIRE_SHARE_SECRET（secretKeyRef）；推送滚动
- [ ] 3.2 生产实弹：published 仪表盘→生成链接→隐身态（无登录）打开渲染→篡改 t=404→归档后链接 404；证据存 reports/
