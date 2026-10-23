## Context

见 `proposal.md` 的 Why。`fd-wire/CONTEXT.md` 已把 cell 的中文领域词定为「隔离单元」，但 `web/src/i18n/zh.ts` 有 19 处直接嵌入裸英文 `cell`。英文侧 `cell` 是原生术语且大量用于复合修饰（cell-free / cell tier / per-cell / old-cell），替换为 isolation unit 会显著劣化英文可读性，且 `CONTEXT.md` 的领域词约定是针对中文表述的。

## Goals / Non-Goals

**Goals**

- 中文界面消除裸英文 `cell`，统一为「隔离单元」。
- 不破坏既有双语键值一致性测试。

**Non-Goals**

- 不改英文侧文案与键位。
- 不改任何后端、API 契约、路由、状态机或权限模型。
- 不新增或删改 i18n 键（只改中文叶子字面量，键集合保持不变）。

## Decisions

### D1：只改中文侧，英文侧保持 `cell`

英文中 `cell` 是原生词且以复合修饰形式承担语法功能（如 `cell-free birth path`、`per-cell storage usage`），逐字替换为 isolation unit 会造出笨重且非常用的英文表述。中文侧本就有既定译名，两侧分别按各自语言习惯处理。

**备选**：两侧都用 isolation unit 以求机械统一。
**不选的理由**：英文侧会造出 `isolation-unit-free`、`per-isolation-unit` 这类非自然表述，为形式统一牺牲可读性。

### D2：复合修饰按中文语序重组，不逐字直译

如 `cell-free` →「免隔离单元」（而非「隔离单元自由」）、`per-cell` →「按隔离单元」、`双 cell 档` →「双隔离单元档」。逐字直译会产生不通顺中文。

### D3：改动面限定在 `zh.ts` 字面量，键位不动

既有 `web/src/i18n/i18nKeys.test.ts` 已强制 zh/en 键集合完全一致；若调整键名，需两侧同步改动。本次只改中文叶子值，键集合零变动，该测试天然不破。

## Risks / Trade-offs

- **[术语在代码注释中另外存在]** `ConsoleView.tsx` / `ArtifactsPage.tsx` 等组件注释里也有「免 cell 免 AI 的出生路径」等表述 → 本次只改呈现文案（用户可见面），代码注释不在本次范围；若一并清理可与本 change 同批做，但注释改动不影响用户可见行为，留作可选。
- **[译名可能与既有运营口径不一致]** 「隔离单元」是 `CONTEXT.md` 既定领域词，非本次新造 → 若实际运营中另有通用说法，以 `CONTEXT.md` 为准（它是术语权威源）。

## Migration Plan

1. 逐处替换 `zh.ts` 中 19 处裸 `cell` 为「隔离单元」，按中文语序重组复合修饰。
2. 跑双语键值测试与前端全量测试。
3. 人工过一遍受影响界面（我的数据/实体/产物/管线/质量/文档页/后台本地数据概览）确认中文通顺。
4. 回滚：单文件文案改动，revert 该提交即可，无数据迁移。

## Open Questions

无。
