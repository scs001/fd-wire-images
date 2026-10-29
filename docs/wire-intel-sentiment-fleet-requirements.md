# 柏讯·情报舆情舰队 需求稿（finddata 会话 → wire 会话交接）

> 来源：finddata 根 agents 集群蓝图探索（2026-10-25，harness 愿景第一批）。
> 配套 change：finddata 根 `add-fleet-shared-packs`（L1 共用 pack 层，五包）+ `add-lex-line-fleets`（lex 三舰，同批样板）。
> 会话归属：**wire 侧产品面配套落 fd-wire 根 change；舰队实例化/市场铸造归 paas 会话**。本稿只提需求与验收口径，不含实现。

## 一、要什么

柏讯情报舆情舰队一支（intel-brief 种子变体），值守柏讯自己的信息面：

| 岗位 | 职责 | KPI 槽位 | 依赖 |
|---|---|---|---|
| 情报巡检员 | 每日巡 fd-info 真流（公告/部委/互动易），要点化去重 | `items_found`/`top_story` | fleet-intel-gather + fd-info |
| 舆情哨 | 每日情绪面异动（sentiment 打分面），预警信号 escalate | `sentiment_shift`/`watch_alerts` | fleet-data-qa + fd-sentiment |
| 情报主编 | 合成晨报，交叉核对两路报数，重大信号拍板提交 | `brief_digest`/`sources_covered` | fleet-brief-report |
| 投稿岗 | 晨报/异动图集落看板草稿 | `drafts_done` | fleet-board-contribution + wire-build |

编导：晨报 trigger（cron）→ 巡检/舆情两岗并行 → handoff 主编 → handoff 投稿岗；舆情哨重大异动另走 escalate 拍板队列。

## 二、为什么是需求稿而不是本会话直接做

- 舰队模板铸造源约定在 `fleet-packs/wire/`（finddata 根，add-fleet-shared-packs 立），但 wire 产品面如有配套改动（如情报看板默认视图、fd-info 源面扩展）归 fd-wire 根 change；
- 实例化与市场铸造在壹座操作面，归 paas 会话（既有会话纪律）。

## 三、验收口径（照内容舰队首日范式）

1. 测试 cell 装共用层 + 本舰模板实例化成功，与 lex 三舰共享同一共用 pack 安装；
2. `*/10` 短 cron 全链真跑：巡检→主编→投稿草稿，source_ref 三元组可追溯；
3. 「未取得」路径演练：拔一路源，晨报如实标注缺位、不编造条目；
4. 舆情 escalate 演练一次，拍板队列留痕；
5. 断言记录落对应 change reports。

## 四、依赖与前置

- **前置**：add-fleet-shared-packs 完成（五包上架、packId 落台账）；
- fd-info / fd-sentiment 现有 MCP 面即用；若情报看板需要新的默认视图，那是 wire 产品面改动，另行立 change；
- 情绪面若需批量打分通道（巡检批量调用的低成本路径），由 wire 侧评估 sentiment pack 的批量口径后在 change 内定。
