# Registry is the wire facade, we build no MCP facade of our own

柏讯对外的 MCP 唯一入口是 registry 上的 wire 入口：鉴权（Logto 身份 + per-user 长命 key）、逐工具调用审计（`MCPServerAccessRecord`）、请求转发三件事全部复用 registry 既有能力——business-mcp 本就在它上面 live，计量源白拿。wire 侧只建薄结算服务：消费 audit 流→按 `rows_returned` 聚合→sub2api 幂等结算。代价认领为两个 fork patch：auth-server 的 per-user 长命 key 铸造/吊销、转发前 sub2api 余额预检探针（fail-closed），走既有 gitee fork 通道。

## Considered Options

- **自建 Python MCP 门面**（wire 仓内起一个 FastAPI MCP 服务承接鉴权/计量/转发）：三件事 registry 全部现成，自建即与 registry 双活维护两套鉴权名单——两处闸门必然漂移（paas ADR-0008 的准入收口教训）；且 business-mcp 已 live 于 registry，门面切换是纯风险无收益。否决。
- **旁路反代计量**（registry 前后挂反向代理论流量数行）：MCP 响应是流式的，代理侧数行不可靠，漏计量直接变计费事故。否决。
- **registry 门面**（本决策）：零新门面代码，工程量收敛到两个 fork patch，patch 以配置开关暴露、灰度可回退。

## Consequences

- wire 线的对外能力面受 registry roadmap 约束：patch 数量封顶两个、尽量以钩子而非侵入实现，patch 大小以 2026-10-03 groups-scope-remap patch 为参照。
- wire 入口与 `mcp.finddatatech.cloud` 内部入口同 registry 不同面：入口故障互不传染，patch 开关关闭即回到 patch 前行为（灰度回归的判据）。
- 预检 patch 若延期，降级预案生效：后置结算 + 周期核对，超支窗口由 80%/95% 两档预警兜底；fail-closed 语义以后端开关分阶段满足。
- 计量源时延=audit 流的消费时延，结算走分钟级批处理窗；硬停不依赖结算时延而依赖预检。
