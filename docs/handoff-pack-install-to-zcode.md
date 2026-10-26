# 交接：柏讯内容舰队装包实跑（ZCode 会话）

> ⚠️ **本文档已于 2026-10-24 下午收口，第二节的装包操作不要再执行**——装包/建队/启动/实跑已由
> 当日晨间会话完成（pack `SWPvacoFTftfVngMMFotwQ`），照本文重装会造出第二支舰队。
> 实际收口状态见文末「六、收口追记（2026-10-24 下午）」。

> 日期：2026-10-24。Claude Code 会话在浏览器装包这步停下（改由 ZCode 的 ego-browser 接手）。
> 本文档 = 当前状态 + 逐步操作 + 验收判据 + 已知坑。

## 一、当前状态（截至交接）

| 项 | 状态 |
|---|---|
| `wire-content-fleet` change | 19/28；validate strict 绿；fd-wire 仓 openspec |
| `add-self-reported-tool-surface` | 9/15；组 1+2 完成 |
| 「柏讯 · 产物构建」pack | **已上架** `AjFHQ9tjiJp5GTYmBC2y8g` v1，匿名可见（市场总数 70） |
| 「柏讯 · 数据治理」pack | **已上架** `8edSwnv3lIXcuSAkzS9vvA` v1 |
| wire-build-mcp 生产 | `sha-16475b4`（finddata-business），自报模块在役，Secret `wire-build-surface` 已注入 |
| registry | digest `d6d32d4d`，自报路由 `/api/surface-report/servers/<path>/tool-list` 在役 |
| registry 快照 | `/wire-build-mcp` 条目 **tool_list=17**（含 dashboard/doc 两新工具），发布门已可零人工过 |
| 待办卡点 | **装包进 cell**（本交接的活）+ 1.6 一周实跑 |

## 二、要做的活：装包 → 建队 → 启动

### 第 1 步：登录壹座

- URL：`https://mcp.finddatatech.cloud/login` → 「寻数科技账号登录」（Logto）
- 测试号备选：`platform-test@findmail.com`（密码见 finddata 记忆 `official-demo-account`）
- **前提**：该账号须在 `wire-customers` 组（柏讯客户层组门，见 pack 的 `required_groups`）。
  若装包报 `requires group: wire-customers`，去 auth-admin 把账号挂进该 Logto 组织。

### 第 2 步：订阅并安装 pack

1. 控制台 → 功能集市场 → 搜「柏讯 · 产物构建」或 id `AjFHQ9tjiJp5GTYmBC2y8g`
2. 订阅 → 安装到运行时
3. 该 pack 自带 3 岗舰队段（fleet），装完应实例化一支**全暂停**舰队「柏讯内容舰队」

### 第 3 步：验收舰队实例化（wire-content-fleet 1.6 前置）

| 检查项 | 期望 |
|---|---|
| 舰队存在 | 名称「柏讯内容舰队」，3 岗 |
| 巡线岗 | 有节奏 `0 9 * * *` Asia/Shanghai，KPI 槽位 signals/dispatched |
| 内容岗 | 被动（无 cron），KPI 槽位 artifacts，父=巡线 |
| 源岗 | 被动，目标位 a2a `packs-kkcie7nlrhluo4likpnn0w-spider-heal` |
| 初始态 | **全暂停**——由安装者显式启动，不自动跑 |
| 服务接线 | wire-build-mcp 在「MCP 市场」可见已连接（市场凭据需用户本人连接一次） |

### 第 4 步：启动舰队（可选项，启动即开始计费）

舰队页 → 启动。巡线岗会在每天 09:00 跑首个 cadence。
**想立即看效果**：手动触发巡线岗一次回合，验证三件事——

1. 回合 prompt 前缀带 `【舰队岗位身份】fleet=…; post=…; title=…`（paas `renderFleetIdentity` 已上线）
2. 巡线岗报数 `report_status`（signals/dispatched 槽位）
3. 空信号时报数写「本轮无信号」且**不调任何写入工具**（3.3 验收）

## 三、已知坑（都是踩过的，别再踩）

1. **SafeLine 拦 spec 写入**：含 `data:` / `javascript:` 的产物 spec 会被公网 PUT 403
   （负例用 `ftp://` 判别）。首产物落库失败先查这个。
2. **装包 402/503**：计费开户三件套缺一——registry caller-map / wire `billing_customers` 行 /
   sub2api 余额。`PREFLIGHT_UNMAPPED=deny` 时是硬闸。
3. **cell 聚焦单会话**：一个 cell 同一时段只能跑一个 agent 回合；巡线岗回合会切走
   用户正在聊的 persona——成本与体验都是已知语义（paas D6），别当 bug。
4. **模型**：`add-fleet-post-model` 尚未上线，岗位模型=cell 默认。巡线岗想用便宜模型得等
   paas 那边 apply 完。
5. **ego-browser 更新提示**：v2.1.0 guide 带了 Ego Lite 更新提示（0.6.0.16 → 新版），
   升级前问用户。
6. **taskSpace 名称**：`wire-pack-install-1024`（spaceId 12）已由我创建又 finish 掉了；
   ZCode 重新 `taskSpace("新名")` 即可，名字不能用已存在的。

## 四、装完之后我来接的活（写给 ZCode 的下一棒）

- 1.6 的「真产物落库 + 连续报数记录」验证（跑一周，产物详情可见、无越权写入）
- 巡线岗空信号比例记录：>80% 则日节奏降每周三次（tasks 1.6 内注）
- 4.2 套件端到端抽查（读→结论→看板草稿，来源指针指向回合）
- 5.x 外部开放（另一客户 cell）
- 6.x 自进化人工门（PR + 三证据）
- 7.4 内外两侧产物契约对账

## 五、关键坐标

```
fd-wire change:        fd-wire/openspec/changes/wire-content-fleet/   （19/28）
surface change:        finddata/openspec/changes/add-self-reported-tool-surface/  (9/15)
pack manifest:         finddata/docs/wire-presets/pack-manifests/wire-artifact-build.json
fleet 模板:            同 manifest 的 fleet 段（3 岗定义在这里）
paas 台账:             paas/docs/registry-maintenance.md §16.1-16.3
需求文档:              fd-wire/docs/paas-*.md（三份：content-fleet / post-model / orchestration）
paas 在途:             add-fleet-post-model（模型链，已立项待 apply）
                       add-fleet-orchestration（多模式编排，处理中）
```

## 六、收口追记（2026-10-24 下午，ZCode 装包交接会话）

**真相**：本文写作时（14:31）并不知道晨间会话（~04:00-05:00）已完成全部装包链路——
pack `SWPvacoFTftfVngMMFotwQ`（04:00 发 v1，后更 v2）已装进 platform-test cell，舰队
`fleet_1792786329045_jchvsd` 已实例化并**启动**（非全暂停），实跑 4+ 轮：内容岗落 36 号
看板草稿（source_ref 三元组全对），五生产缺陷当日全修（详见 memory
`wire-content-fleet-live-run-2026-10-24`）。本文档第二节所述「装包四步」已无必要执行。

**下午收口动作（ZCode 会话，用户授权）**：

1. **两件 HIGH 审批已裁**（fleet 审批队列清空）：
   - 08:09 凭据单：批「刷新凭据」。实际处置=壹座连接面板「柏讯操作」静默重铸
     console token（有效至 10-24 23:17）；自动续期 add-line-credential-renewal
     sha-f8f1505 已上产。产物 500 已修（api sha-8b6a68b + 生产 PG 0040）。
   - 05:41 空转单：批「改道兜底」选项——数据获取类信号改派内容岗产出「缺口说明文档」，
     文档须如实标注源岗 a2a 不可用（spider-heal 萬星私有包未对本 cell 开放）。
     换 target + 固定 dispatch edge 归 add-fleet-orchestration（在途）与萬星侧开放评估。
2. **市场重复清理**：`AjFHQ9tjiJp5GTYmBC2y8g`（11:37 建重产物，与 SWPvaco v2 逐字节一致）
   已**下架**。保留 SWPvaco——铁证：`fleet.sourceTemplate.packId=SWPvacoFTftfVngMMFotwQ`。
   ⚠️ 同类历史建重残留还有三对（柏讯 · 预期差/政策传导/信息冲击 各有 v1 旧 id 在架），
   未清理——哪个 id 被客户 cell 装机未验证，盲下架会断升级路径，动前先查装机血统。
3. **交接文档状态修正**：第一节状态表「待办卡点=装包进 cell」不成立；第 3 步检查表中
   「初始态全暂停」与事实不符（舰队已启动，1.6 观察期已在跑）。
4. 巡线 promptTemplate 已升级（禁 HTTP 探测/信号分类派发/5 分钟收尾），每日 09:00 下一轮
   （2026-10-25T01:00Z）起按新模板+改道裁决运行。

**遗留**：orchestration 复位 bug（复位者未定位，复发按 `PATCH /api/fleets/:id
{"orchestration":{"mode":"supervisor"}}` 恢复）；fleet cell 子进程凭据 P2 时序缝隙
（401 不消先重启 platform）；4.2 抽查 / 5.x 外部 / 6.x 人工门 / 7.4 对账照旧。
