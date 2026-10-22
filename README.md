# 柏讯（Wire）

寻数五线（壹座/识律/柏讯/谦面/萬星）中 **wire 线的平台仓**：B2B 数据平台——公开官网、
客户控制台、内部后台三面。单域 `wire.finddatatech.cloud` 三入口：`/` 官网、
`/console` 控制台、`/mcp` API 入口（反代 registry wire 入口）。

2026-10-04 定案：wire 线从「私有镜像交付一个 MCP 服务器」升级为 B2B 自助数据平台
（客户自助注册→领长命 key→浏览指标目录→构建分析→提数据需求，全链路计量计费）。
架构决策起步三条：[ADR-0001](docs/adr/0001-registry-is-the-wire-facade.md)、
[ADR-0002](docs/adr/0002-cell-is-the-enterprise.md)、
[ADR-0003](docs/adr/0003-metering-attribution-is-dual-credential.md)。

## 本仓现在有什么

| 目录 | 职责 | 状态 |
|---|---|---|
| `web/` | 官网 + 控制台前端（React19 + Vite6 + TS + Tailwind4 + echarts + i18next 双语） | 骨架 |
| `api/` | FastAPI 服务（`fd_wire_api`：结算/后台 API 起步） | 骨架 |
| `cell-manager/` | 企业 cell 生命周期（开通/暂停/恢复/注销、容器+SQLite 卷、注册 registry） | v1 仅骨架 |

## 借用契约（不归本仓，经显式契约访问）

- **business-mcp**：五库联邦数据读取工具（`rows_returned` 计量戳），`BUSINESS_MCP_URL`
- **谦面 registry**：wire 入口——鉴权、逐调用审计、转发（ADR-0001），`REGISTRY_URL`
- **sub2api 计费面**：客户账户平台代管、余额与结算，`SUB2API_BASE`
- **DAAS 引擎**（fd-daas-mcp）：cell 内运行，零多用户化（ADR-0002），经 cell 编排
- **token 网关**：AI 构建循环（token.finddatatech.cloud），`TOKEN_GATEWAY_URL`

对兄弟线与外部组件只经 HTTP 契约访问、env 可重指——这条纪律与 fd-wanxing 同款，
守住它才有跨线互联而不互相阻塞发布。

## 相关

- 治理：finddata 母仓 `openspec/changes/wire-platform-v1/`（specs 随代码成长后迁入本仓）
- 品牌与五线语言：`~/finddata/CONTEXT.md`
- 同范式参照：`fd-wanxing`（萬星独立仓）
