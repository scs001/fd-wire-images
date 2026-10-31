# xingyan-content-supply — 星研内容进柏讯供给面

## Why

星研内容的读者结构已从「人读页面」扩展到「AI 读供给」：客户侧 agent 经 MCP 消费柏讯目录的数据，但内容面（内参、学院、研究）在供给面上为零——客户 agent 想引用星研的方法论断言，只能爬官网 HTML，claim 骨架与证据链全部丢失。姊妹 change `xingyan-claim-layer`（finddata 根）把内容做成机器可验的 claim+evidence 结构并经三区 feed 对外；本 change 是消费侧收口：把星研内容作为一等供给对象进柏讯目录，客户 agent 以 claim 级粒度发现、检索、取证据。这也是「可信、可溯源、可审计」主张的 dogfooding——内容自己走自家供给面。

## What Changes

- **快照摄取作业（新）**：定时+按需从官网三个 feed 端点（经集群内网地址）摄取条目与 claims 骨架；快照带来源指纹与摄取时间；重跑同版本幂等；上游不可达时保留上一版并暴露陈旧标记（静默空结果被禁止）。
- **目录供给（新）**：已摄取条目成为客户域目录一等记录（slug、双语标题/摘要、日期、正文指针、claims 集），默认可见档公开，按既有档位可见性治理可控。
- **claim 级地址化（新）**：全局 ID `slug/claim-id` 在供给面可解析到骨架+锚小节+证据集；双语指向同一 claim；已撤销 claim 返回显式撤销态而非消失。
- **MCP 工具面（新）**：显式声明的窄工具面（列目录、检索、claim 取回），工具响应携带指纹与 staleness；绝不以未声明全量形式暴露（工具面治理先例：未声明=全量）。
- **漂移对账（新）**：export/reconcile 双闸只读比对 feed 快照、目录记录、claim 索引三处，输出差异清单。

### 不做（Non-goals）

- 不改接口真源：feed schema 与全局 ID 口径以 `xingyan-claim-layer` 的 spec 为准，本 change 只引用不重定义。
- 不做内容写回/客户投稿通道：供给面只读。
- 不做计量计费新档：内容读取走既有档位闸，不为本内容加白名单机制。
- 不动 wire-content-fleet（那是自家创作流水线，与此消费面正交）。

## Capabilities

### New Capabilities

- `xingyan-content-supply`: 星研内容供给域——快照摄取（幂等、指纹、staleness）、目录一等供给与可见性、claim 级全局 ID 解析、显式声明的 MCP 工具面、只读三处对账。

### Modified Capabilities

（无——挂载走既有机制：源包/目录切片、可见性档、工具面声明与刷新均为在册能力的既有用法；apply 时若发现目录切片机制不足以承载文本正文，再以 delta 扩对应 spec，不在本提案预设。）

## Impact

- **服务面**：fd-wire 业务域（摄取作业+存储表+对账脚本）与网关工具面（新工具注册+显式 scope 声明）。无新增服务。
- **依赖**：前置=`xingyan-claim-layer` 组 2/3 产出三 feed 与 claims 骨架并全库回改收口；对账基准=其 `content-manifest.json` 指纹。
- **部署配方**：镜像须落 `internal-main` 才进镜像（在册结论）；registry 工具面刷新走 `PATCH /api/servers/<path>`（在册唯一可用通道）；部署后须重烘焙 registry 快照才能发 pack（AuthGate 401 探不到）。
- **风险**：① fd-wire 有并发会话历史，apply 前查 `openspec list` 避让；② feed schema 在姊妹根演化——本根只读侧做版本断言，不匹配即拒摄而非猜字段；③ 集群内回环有系统代理劫持先例，抓取走 mesh 内网域名并显式 NO_PROXY。
