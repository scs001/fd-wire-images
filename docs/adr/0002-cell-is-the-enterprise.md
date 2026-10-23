# The cell is the enterprise: process + data root, zero multi-user changes to the DAAS engine

企业客户的隔离单元是 cell——一个独立服务进程 + 一个独立数据根，与 paas ADR-0008 同一哲学：数据文件之上还有 agent 运行时（对话、上下文、cron、委派），单进程服务所有人时进程内 ACL 治不了这层共享，进程边界是唯一覆盖全部泄漏面的隔离原语。因此 DAAS 引擎（fd-daas-mcp，161 工具）保持单用户性质、代码里零 tenant 概念——柏讯以「每企业一个 cell」接入引擎，而非把引擎多用户化；这与謙面本地产品保持同源，升级即滚动替换 cell 容器镜像。v1 形态=容器 + 独立 SQLite WAL 卷（备份=快照、迁移=拷文件）；长大后 `DAAS_DATABASE_URL` 重指专属 PG database——先 spike 后迁移，不提前做。平台侧永不直读租户 daas.db，跨租户数据全在平台库。

## Considered Options

- **tenant 列进程内 ACL**（给引擎的表与路由加 owner/tenant 列做虚拟隔离）：要改穿 161 个工具与存储层，且单进程内的运行时共享（缓存、句柄、调度）仍需另设闸门——治标丢本；与謙面本地产品永久分叉，一套引擎两种真相。否决（引 paas ADR-0008 同哲学）。
- **每企业独立部署整套平台**（连控制台/计费面一起复制 N 份）：运营成本随客户数线性上涨，计量与账本被切成孤岛，B2B 自助平台叙事不成立。否决。
- **cell=企业**（本决策）：引擎零改动、隔离原语一个（进程+数据根）、cell 开通即注册进 registry 同门计量。

## Consequences

- 需要 cell manager 管生命周期：开通/暂停/恢复/注销，容器与 SQLite 卷随动；注销保留期可配（默认 30 天），到期清除留档。
- cell 是「接入而非重建」的品牌正当性基础：同一 DAAS 引擎、謙面本地自托管 / 柏讯云端托管两种交付，引擎侧永远一份代码。
- SQLite→PG 迁移以 `DAAS_DATABASE_URL` 为预留缝：DAAS SQLAlchemy 层的 JSON 函数/方言兼容性未验证前，迁移任务不进任何排期。
- 隔离边界在 cell，准入收口在身份提供方（Logto organization）：wire 入口最小 scope 只放行 business-mcp 与本企业 cell，内部 server 条目对外不可见。
