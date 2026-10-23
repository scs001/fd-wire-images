# wire-share-ttl-options

## Why

分享链接当前固定 30 天——分享方常需要更长（季度汇报 90 天）或更短（临时演示
7 天）的窗口。小改：生成时自选有效期。API 已支持 TTL 透出，只缺选项面。

## What Changes

- **API**：`POST /api/v1/artifacts/{id}/share` 接受可选 `ttl_days`（枚举
  7|30|90；缺省 30）；非法值 422 列合法值。
- **web**：分享弹层加有效期选择（7 天/30 天/90 天三选一，默认 30），到期时间
  随选联动；i18n。

## Capabilities

### Modified Capabilities

- `dashboard-share`: 生成分享链接时可选有效期（7/30/90 天枚举，缺省 30）。

## Impact

- api（share 路由+测试三态）、web（弹层+测试）、i18n。零迁移。
