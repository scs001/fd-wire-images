## Why

`fd-wire/CONTEXT.md` 明文将 cell 的中文领域词定为「隔离单元」，并列出禁用词（租户/实例/workspace）；但 `web/src/i18n/zh.ts` 中 19 处文案直接嵌入裸英文 `cell`，中文界面因此出现「免 cell 免 AI 的出生路径」「cell 层」「cell 生命周期」「客户（cell）」「交付进入私有范围（cell）」等半中半英表述。英文侧 `cell` 是原生术语且多用于复合修饰（cell-free / cell tier / per-cell / old-cell），强行替换为 isolation unit 会显著劣化可读性，故英文侧保持不动。

## What Changes

- 中文界面文案中裸英文 `cell` 一律改为「隔离单元」（属 `CONTEXT.md` 既定领域词，非新造词）。
- 涉及键位包括：`docs.forms.cellLabel`、`docs.entities.*`、`docs.demandIntake.*`、`datasets.cellTier`、`datasets.storage.cellLifecycle`/`tier.cell`、`pipeline*.tier.cell`、`quality.unsupportedHint`、`admin.colCustomer` 与相关叙述句；逐处按中文语序重组（如「双 cell 档」→「双隔离单元档」、「per-cell」→「按隔离单元」）。
- 复合修饰语按中文习惯处理，不做逐字直译（如 `cell-free` →「免隔离单元」）。
- 英文侧术语与键位保持不变。
- 双语键值一致性由既有 `web/src/i18n/i18nKeys.test.ts` 覆盖（键集合一致 + 叶子非空 + 无手写指标数字），本次改动 SHALL 不破坏该测试。

## Capabilities

本 change 为纯文案收敛：术语替换不改变任何对外可观察行为（不改 API、不改状态机、不改路由、不改权限模型），故不引入也不修改任何 capability 的 requirement。

## Impact

- 前端：`web/src/i18n/zh.ts`（仅该文件的文案字面量）。
- 无后端、无 API 契约、无数据库变更。
- 受影响界面：控制台我的数据/实体/产物/管线/质量、文档页（表单/实体/需求单章节）、内部后台本地数据概览表头。
