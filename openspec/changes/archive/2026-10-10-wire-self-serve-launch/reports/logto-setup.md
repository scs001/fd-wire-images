# 4.1 Logto 手工配置记录（2026-10-09，ego-browser @ Admin Console）

> Logto v1.42.0 @ auth-admin.finddatatech.cloud（Admin Console UI 通道——M2M /api/v2 仍 404，与 lex/facet 同纪律）。

## 1. SPA 应用 wire-console

- **App ID**: `i0oxdswqxv4x7te99iukv`（类型 Single Page App）
- Redirect URI: `https://wire.finddatatech.cloud/console/callback`
- Post sign-out redirect URI: `https://wire.finddatatech.cloud/console`
- CORS allowed origins: `https://wire.finddatatech.cloud`
- Refresh token: **Always issue refresh token = ON**（=lex 样板 always-refresh；web 静默续期依赖）；TTL 默认 14 天
- 应用下线路径（部署侧）：client_id 已入 Secret `fd-wire-env` 键 `fd_wire_logto_client_id`，api 已重启，`GET /api/console/auth/config` 实证 `configured:true` + 四字段契约逐字

## 2. 角色 wire-admin

- Role ID `pzczoe21b9pkbhq7tzpnc`，类型 User，零权限（与 lex-admin 同款——双闸第二道是 X-Admin-Token，角色只做身份门）
- **已指派：aloadtree@gmail.com**（壹座管理员号，panel-user 同源身份）

## 3. 验证锚点

- `/api/console/auth/config` → `{"configured":true,"issuer":"https://auth.finddatatech.cloud","client_id":"i0oxdswqxv4x7te99iukv","scopes":[openid,profile,email,roles,offline_access],"organization_required":false}`
- wire-console 登录流（4.7 E2E）见后续报告

## 备注

- 有意**不要求 organizations claim**（design D2：绕开 M2M 不可用导致的挂组人工环节；授权面走 wgk- key 组快照，不经 Logto 组）
- 测试身份 wire-demo@findmail.com（wiredemocustomer，default tenant）密码未在档——E2E 前经 Admin Console 重置
