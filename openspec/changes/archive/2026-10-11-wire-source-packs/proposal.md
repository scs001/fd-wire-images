# wire-source-packs

## Why

方向 D（个人信息工作台）的目标是「终端用户处理数百个信息源」——个人不订"指标"
订"主题包"。指标目录（57,553）已有 domain/source_db 结构，缺一层**源包**：按
主题（宏观价格/人口就业/行业景气…）打包目录切片 + 订阅态 + 控制台「源包」页。
这是 D 方向剩余两件（源包订阅 + 薄工作台）中的订阅件。

## What Changes

- **源包定义（服务端静态配置）**：`fd_wire_api/packs/definitions.py`——首批
  6-10 个包，每包 `{id, name_zh, name_en, desc, filter}`，filter 为
  {domain|source_db|codes} 的目录查询条件；包的指标清单实时按 filter 从
  indicator_snapshots 算（不落库，目录即真相）。
- **API**：`GET /api/v1/packs`（公开，含各包指标数）；`GET /api/v1/packs/{id}`
  （公开，包详情+分页指标清单）；`GET /api/v1/packs/subscriptions`（登录，
  我的订阅）；`POST/DELETE /api/v1/packs/{id}/subscribe|unsubscribe`（登录，
  写 `pack_subscriptions` 表：customer_id+pack_id 唯一）。
- **控制台「源包」页**：左侧栏新增「源包」tab——包卡片墙（名称/描述/指标数/
  订阅态），包详情抽屉列指标（点击跳指标目录详情页）；「我的订阅」过滤视图。
- **i18n 双语**；免费档可用（目录读不过计量轨道，订阅免费）。

## Capabilities

### New Capabilities

- `source-packs`: 源包定义（静态配置+目录实时切片）、公开包列表/详情、登录
  订阅/退订与我的订阅、控制台源包页。

## Impact

- **fd-wire/api**：packs 模块（definitions+service+routes）、alembic 0022
  （pack_subscriptions 表）、pytest。
- **fd-wire/web**：ConsoleLayout 导航+PacksPage（卡片墙/详情抽屉/订阅切换）、
  i18n、组件测试。
- **不碰**：指标目录本体、计量计费（源包读走公开目录通道）。
