## Context

见 `proposal.md` — Why。三个已实测的约束塑造本设计：

1. **上游带内容创建限平台租户**：`artifact-lifecycle` 现行要求「带 spec 创建仅限 `finddata-official`，其他租户一律 403」。外部客户直接一步建带内容产物会被挡。
2. **`wire-build-mcp` 已有解法**：form/record-view 走「空白建档 + spec 写入」两步，第二步失败时归档空白产物补偿。这条路对全租户可用，不动上游政策。
3. **舰队机制在壹座，不在萬星**：pack 的 `fleet` 段 → `provisionFleetFromTemplate` → 岗位/节奏/KPI；目标位支持 `existing-a2a`，可绑定已部署的萬星 Agent Service。爬虫自愈链已用它跑通。

实施前已实读核对的四个锚点：

- `wire-build-mcp/tools_console.py::_create_with_spec` 已实现「空白建档 → PUT 完整 spec → 失败补偿归档」，对全租户可用
- `fd-wire/api/.../artifacts/routes.py::create_artifact` 的 403 只在 `payload.spec is not None` 时触发；两步路径完全不经过它
- `paas/server/fleet-template.js:147` 与 `fleet-provisioner.js:68-71` 已支持 `existing-a2a` 目标位，解析走 `resolveExisting({kind:"a2a", id})`，且 a2a 不参与岗位冲突占位
- `fd-wire/api/.../artifacts/models.py:122` 版本行已有 `operator`（可空文本，console 身份语义）；`source_ref` 作为并列新列，不复用它

## Goals / Non-Goals

**Goals:**

- 产物构建 MCP 面覆盖 dashboard 与 doc，与既有 form/record-view 同一纪律
- 同一份 pack 同时服务内部狗粮与外部客户，差异只在模板/凭据/信号
- 无人值守舰队的成本有硬边界（信号驱动 + 既有配额 + 凭据轨）
- 产品内容自进化止于 PR 人工门

**Non-Goals:**

- 不新建 agent 运行时、调度器或事件总线
- 不改 `refresh-scheduler` 的间隔地板与配额语义
- 不放开「带内容一步创建」的平台租户限制
- 不让 agent 自动 merge 或点亮任何调度
- 不为内部/外部各做一份 pack

## Decisions

### D1 · 产物创建走两步 + 补偿，不碰上游 403

新工具复用 form/record-view 的路径：空白建档（全租户可用）→ spec 写入（全租户可用）→ 失败则归档空白产物。

**替代方案**：放开 `artifact-lifecycle` 的 `finddata-official` 限制，让 MCP 一步带内容创建。**否决**：扩大滥用面而无收益——两步路径已验证可用，且零半写由补偿保证。

**代价**：多一次上游往返；失败时多一次归档调用。可接受。

### D2 · pack 分三型，一个 pack 可同时是多种

```
只读分析型    skills + 只读 mcpServers              （现有 22 个）
产物生产型    skills + wire-build-mcp               （新增 2 个）
舰队型        任意 pack + fleet 段                   （新增模板 A/B）
```

`fleet` 段与 `mcpServers` 正交：一个 pack 可以既声明构建服务又带舰队模板。不带 `fleet` 段时行为与现状逐字节一致（`fleet-template.js` 已在缺段时短路）。

### D3 · 舰队是壹座舰队，萬星只提供 A2A 服务位

爬虫构建岗位的目标位用 `existing-a2a` 绑定已部署的 spider-heal，不新写 agent、不新建编排层。萬星侧零改动。

**替代方案**：在萬星侧新建「集群」概念。**否决**：与壹座 `集群`（平的成员组）撞名，且舰队已具备组织树/节奏/报数/拍板全部所需能力。

**岗位编排：3 岗，1 主动 + 2 被动**

```
巡线岗   主动 · 日节奏       读信号 -> 决定本轮做什么 -> 委派 / 报数
内容岗   被动 · 无节奏       被委派才起：看板 + 文本（wire-build-mcp）
源岗     被动 · 无节奏       被委派才起：existing-a2a = spider-heal
```

- **为什么不是 2 岗**：内容岗写产物（草稿优先、发布独立），源岗写代码 PR（分支 + 验证链 + 人工 merge）。两者的写入面、失败纪律、失败代价差一个数量级，合成一岗后出错无法分诊。
- **为什么不是 4 岗**：不设审校岗。内部场景人工 merge PR 就是审校，外部场景发布前那道门是客户自己；再加一个审校岗等于把人工门换成机器门，与 D7 冲突。报数不是岗位，是每个岗收尾的工具调用。
- **数据治理不进第一版舰队**：`wire-data-steward` pack 照常发布（可装给人用），但不设岗。改客户数据是全部动作里风险最高的一个，等前三岗跑顺、来源标记纪律被真实验证后再加。
- **成本量级**：巡线 30 回合/月（多数空信号早退，1-2 分钟）+ 内容 ~8 回合/月（4 分钟）+ 源 ~4 回合/月（5 分钟）≈ 97 分钟/月/舰队。四岗全主动日跑是 120 回合/月，约三倍。
- **可调项**：巡线岗日节奏可能偏勤。第一版按日跑一周，空信号比例 >80% 就降到每周三次（落配置，不改契约）。

### D4 · 节奏由信号驱动，实现靠 prompt 注入 + 空信号早退

岗位 `promptTemplate` 注入信号摘要（覆盖缺口、质量快照、需求单、告警）；岗位回合开始先判「本轮有无信号」，无信号则报数「无信号」并结束，不调用任何写入工具。

**替代方案**：建信号→回合的事件驱动调度器。**否决**：新常驻组件，且壹座 cron 已能承载节奏。

**代价**：空回合仍消耗一次短回合的分钟数。用长节奏（日/周）与信号阈值压住。

### D5 · 周期物化归 `refresh-scheduler`

舰队只判断「内容该不该变」；变则更新产物 spec，物化由既有刷新调度或显式调用完成。间隔地板（dataset ≥60 分钟、artifact ≥360 分钟）与每客户配额直接复用，不新造预算机制。

### D6 · 生产来源指针用新增可选列，不复用 operator

产物版本行新增可空 `source_ref`（部署标识 / 岗位标识 / 回合标识），append-only。不复用既有 `operator` 字段——那是人工身份语义，混用会让审计读错。来源不参与鉴权（客户域隔离只看归属）。

### D7 · 产品内容自进化照抄 spider-heal 的人工门

分支 PR + 验证证据（`check_presets.py`、`pack-server-closure`、真校验器）+ 人工 merge。自动路径不 merge、不点亮、不改范围外文件。

**范围限定**：第一版只允许改**技能稿正文**；套件 manifest 与示例产物的改动单列为后续增量（动的是已发布 pack 的引用面，风险高一级）。

## Risks / Trade-offs

- **[无人值守成本失控]** → 信号驱动 + 日/周长节奏 + 主人池（内部）/客户凭据（外部）+ 刷新调度既有配额；平台补贴不覆盖周期回合。
- **[内部/外部模板漂移]** → 产物契约共用同一条校验与渲染路径；模板差异被限定为岗位编排与节奏，spec 层不允许分叉。
- **[自进化改坏已发布 pack]** → 第一版只改技能稿正文；manifest 改动过 `pack-server-closure` 且人工 merge。
- **[跨根 spec 分裂]** → 本 change 在 `fd-wire` 根；`wire-build-mcp` 与 `wire-analysis-presets` 两份 spec 在工作区根，同步阶段需单列跨根条目，否则工具面契约会双写漂移。
- **[空回合仍然烧钱]** → 用信号阈值与长节奏压住；若实测空回合占比高，再把早退前移到调度层（不改本设计其余部分）。

## Migration Plan

1. **零代码先证闭环**：2 个 pack（现有 15 工具 + 2 篇技能稿）+ 各一个 2 岗位舰队模板，装进运营号 cell 跑一周。
2. **补 MCP 工具**：`wire_artifact_dashboard_create` / `wire_artifact_doc_create` + 来源指针列 + 迁移。
3. **套件升级**：22 个分析套件 manifest 追加 `wire-build-mcp` 声明（可选，不影响只读路径）。
4. **外部开放**：舰队模板 B + 客户凭据轨 + 需求单/刷新接线。

**回滚**：pack 卸载即止；MCP 工具与来源指针均为新增，移除工具不改变既有工具面；来源列可空，回滚时无需回填。

## Open Questions

- 舰队模板 A/B 的岗位数（2 岗 vs 4 岗）按实测成本定，不影响契约。
- 信号阈值（几次缺口/多少条异常才起回合）属调参，落配置不落 spec。
