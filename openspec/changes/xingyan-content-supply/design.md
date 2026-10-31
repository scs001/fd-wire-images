# design — xingyan-content-supply

## Context

上游（姊妹根 `xingyan-claim-layer`）产出三区 feed（`/briefings.json`、`/academy.json`、`/research.json`，载荷同构含 claims 骨架）与 `content-manifest.json` 指纹。本根在册机制：目录/实体层（entity-layer，语义一等对象）、源包（source-packs，服务端按 filter 实时切目录、不落库副本）、可见性四档（dataset-marketplace 先例）、工具面显式声明（mcp 治理：未声明=全量）、刷新调度（refresh-scheduler）、export/reconcile 双闸（lex 先例）、CronJob 停机不补跑（10-26 实祸教训）。文本正文与指标行两种数据形态在目录里的承载差异，是本设计最大的不确定点。

## Goals / Non-Goals

**Goals:**
- 客户 agent 三步可达：目录发现 → 条目检索 → claim 取回，每步带指纹。
- 摄取链路单向、可重放、可拒绝（版本断言），对账只读。

**Non-Goals:**
- 不定义 feed schema（真源在姊妹根）；不做正文写回；不新建可见性特权；不动 wire-content-fleet。

## Decisions

### D-S1 承载形态：独立快照切片（不并入 catalog 表、不伪造数据集行）

> **apply 期修正（2026-10-31 勘验结论）**：原设计写「以 entity 声明 `xingyan_article`（绑定数据集+主键列）」。实测否掉两条路：
> - **entity/资产目录路**：`assets`/`market` 两表强绑 `datasets.id` + 客户 `customer_id`（`assets/routes.py:386,418-422`），且其可见性四档 `private|link|directed|market` **刻意不含"公开"**（红线 3：公开面不得含客户数据）。平台级公开内容若走这条路，必须伪造一条 Dataset 行并借客户隔离语义——两头都错。
> - **catalog 表路**：`IndicatorSnapshot` 是"指标注册表的镜像"，其 `MirrorWriteBlocked` guard（`catalog/db.py`）与 `_SYNC_FIELDS` 封闭元组是为单一权威源定制；混入第二个权威源会让一条写保护纪律承担两种含义。
>
> 故落**独立 `xingyan` 切片**：自有 `Base` + 三表（`xingyan_items`/`xingyan_claims`/`xingyan_sync_state`）落元数据库、同一条 alembic 链追加 0046；语义与 catalog 同构（权威源只读镜像，唯一写入口=`XingyanFeedSync`），但表与 guard 各自独立。**因此不需要 entity-layer MODIFIED delta**——在册目录机制未被改动，本切片是新增面而非改写既有面。

### D-S2 版本断言与幂等：按 manifest 指纹短路

每次摄取先取 `content-manifest.json`；指纹与表内当前版本一致 → 直接跳过（幂等）。不一致才拉三区 feed 全量 upsert（键=slug；claims 集整体替换该 slug 行）。schema 兼容判定读 feed 顶层 `schema_version` 字段（姊妹根收口时若未带该字段，组1 接口核验任务里先与姊妹根对齐补上——接口真源仍是其 spec，本根只做断言不重定义语义）。

### D-S3 撤销语义：feed 缺席即候选撤销，对账定夺

上游撤稿=从 feed 消失。摄取时对「表内有、feed 无」的 slug 标 `withdrawn`（不删行），其 claims 同步转撤销态；MUST NOT 单次缺席即物理删（防上游半版本误伤，与 D-S2 的全量原子替换配合：只在三区全部拉取成功时做缺席判定）。

### D-S4 抓取通道：集群内网地址 + NO_PROXY

官网在 chengsi k3s（30442←EdgeOne），本根作业在 cheap 集群——跨集群抓取走 mesh 内网域名，显式 NO_PROXY 防本机代理劫持先例（502 假故障）；凭据零需求（feed 公网件），仍按源码零口令纪律不落任何密钥。

### D-S5 节奏：每日 CronJob + 手动刷新端点，对账兜底补跑

每日一次 + 既有 refresh-scheduler 按需；CronJob 停机不补跑教训 → 对账脚本兼任「发现落后」通道（差异=落后证据），运维可据此手动触发，不建复杂补偿调度。

### D-S6 工具面与部署链

工具三件（list/search/claim 取回）显式声明进 server tool 面，`PATCH /api/servers/<path>` 全量换 tool_list（在册唯一通道）；镜像须落 `internal-main` 才进镜像（在册结论）；部署后重烘焙 registry 快照才能发 pack（AuthGate 401 探不到）。验收在线探面用 registry pod 内 initialize+tools/list 配方（在册判据=「已接线」唯一硬证据）。

## Risks / Trade-offs

- [目录注册路径与文本形态不匹配（D-S1 最大不确定点）] → apply 组2 首任务即勘验 entity/目录注册对文本正文的承载，不足则以 MODIFIED delta 扩 entity-layer spec 再落库——本提案不预设，勘验结论进 tasks。
- [上游 feed 演化未同步版本断言 → 消费面僵化] → 版本不匹配拒摄+留痕，拒摄告警指回姊妹根接口；宁僵不猜。
- [撤销误标（半版本缺席）] → D-S3 三区全成才判缺席+withdrawn 可逆（再现即回 active），对账报告含撤销清单供人工复核。
- [fd-wire 并发会话历史] → apply 前 `openspec list` 避让、镜像/部署步骤与在产变更错窗（在册教训）。

## Migration Plan

1. 姊妹根收口（三 feed+claims 载荷+manifest 公网可达）为硬前置。
2. 快照表+摄取作业+版本断言先行，跑两轮幂等对账。
3. 目录注册与可见性挂档（依 D-S1 勘验结论）。
4. 工具面注册+显式声明+registry 快照重烘焙。
5. 对账入调度，全量验收（含在线 tools 探测与陈旧演练）。
6. 回滚：工具面摘除+作业停用即回零接入；表数据留档无害。

## Open Questions

- 计费档位归属：内容读取默认走免费档既有闸；若后续要限流（正文全量拉取成本），在 free-tier-data 既有闸上加，不改本供给面语义。
- pack 分发：是否出一个「星研内容伴学/检索」pack——待供给面真跑通后另议（涉及 facet pack 发布门，非本 change）。
