# Proposal: wire-rich-text-docs（富文本文档：doc 产物 + Markdown 分栏编辑器 + 文件服务插图）

## Why

客户要在柏讯写**报告/文档**（客户侧，富文本形式）——这是柏讯要处理的业务之
一，也是「把 CMS 能力补进柏讯」定调的落点。当前产物体系有七类 kind，但没有
一类能承载「一篇人写的长文档」：

- **dashboard/form/record-view** 是结构化数据面（面板/字段/记录），不是叙述性
  文本；
- **research_report** 是 AI 生成的报告产物（无人工编辑面，spec 是结构化
  data_scope，不是正文）；
- 现有编辑器 `SpecViewer` 是**裸 JSON textarea**（`ArtifactsPage.tsx:1201-1402`），
  给不了写作体验。

三个结构缺口：

1. **没有正文型产物**：需要一类 kind，正文是一等公民（人写、可长、可迭代）。
2. **没有写作界面**：全站零富文本/markdown 依赖（`web/package.json` 无
   tiptap/slate/marked 等；`dangerouslySetInnerHTML` 全仓零命中）——这是
   柏讯第一个 WYSIWYG 面，渲染安全必须作为一等契约设计，而不是事后补锁。
3. **插图无通道**：柏讯零文件能力；平台文件服务（`fd-file-service`，独立
   仓，owner 域隔离 + 预签名直传 + 取用时效地址）刚建成——插图的正确归宿
   是它，而不是柏讯自建上传。

## 决策记录（grilling 定案，2026-10-16）

> 诚实说明：本轮 grilling 的原始问答记录未能完整回收（会话上下文已被压缩，
> 留存证据只覆盖其中 8 项）。下表 1-8 项为**原文照录**；9-12 项为**由前 8
> 项与仓库事实推导的重建项**，逐条标注依据——评审时如有出入直接改这几行。

**原文照录（8 项）**：

| # | 决策 | 含义 |
|---|---|---|
| 1 | 文档形态 | 新增正文型产物 kind，正文为人写的富文本 |
| 2 | 空白创建白名单 | 该 kind 进空白创建白名单（对齐 form/record-view 先例） |
| 3 | md 分栏 | 编辑形态=Markdown 分栏：左写 md、右实时预览 |
| 4 | 草稿层与版本链分离 | 自动保存落**草稿层**（高频、覆盖写）；显式「保存版本」才追加进 append-only 版本链 |
| 5 | 可分享 | 走既有 share 机器（签名令牌、钉死版本、TTL 档） |
| 6 | 不进市场 v1 | 不加入 `MARKETABLE_KINDS`（服务端 422 即门） |
| 7 | 外链图片优先 | 插图 v1 用**外链 URL**；文件服务上传通道后置 |
| 8 | XSS 硬底线 | 正文渲染的 XSS 防护是不可协商的硬约束 |

**推导重建（4 项，依据附后）**：

| # | 决策 | 依据 |
|---|---|---|
| 9 | **正文存 Markdown 源文本**，不存 HTML | 决策 3（md 分栏）意味着 md 是事实源；存 HTML 会锁死渲染器且放大 XSS 面（决策 8）。渲染每次由 md 源现渲 |
| 10 | **正文渲染零第三方富文本依赖**：自研最小 markdown 子集渲染器（标题/粗斜/列表/引用/代码/链接/图片），渲染成 React 节点（不用 `dangerouslySetInnerHTML`） | 决策 8 的最强形态：不产生 innerHTML 即无可注入点——XSS 由「事后消毒」升为「结构上不可达」；且与仓库零依赖现状一致 |
| 11 | **正文体积上限 256 KB**（超出 422） | 仓库现状无任何 spec 体积限制（探索确认），文档正文是首个「可长」载荷，必须补上限（对齐 demand workorder `content` 65536 的先例精神，取其更宽值） |
| 12 | **归属域与权限沿用 Artifact 现有模型**（`customer_id` 域隔离 + console 角色门），不新增 ACL | 探索确认全仓无 per-artifact ACL，record-view 先例即「镜像 Artifact 而不发明新模型」；文档与看板同权 |

## What Changes

- **新产物 kind `doc`**（artifact-lifecycle 白名单 v0 +`doc`，空白创建）：
  spec 信封 `{title, spec:{format:"markdown", text:"<md 源>"}}`。
- **草稿层**（决策 4 的载体）：新增 `artifact_drafts` 表（`artifact_id` 唯一、
  `text`、`updated_at`、`updated_by`）——自动保存覆盖写这一行；显式「保存
  版本」才把草稿层内容写进 `artifact_versions`（append-only 语义原样不动）。
  草稿层不进版本链、不进 marketplace 判定、不影响 share 钉版语义。
- **编辑器**：`web/src/console/doc/` 分栏面——左 Markdown textarea（受控）、
  右实时预览（自研渲染器输出 React 节点）；顶部「保存版本」与自动保存状态
  （已保存/保存中/未保存）；版本抽屉复用既有组件看历史。
- **渲染器**：`web/src/console/doc/md.tsx` —— 最小 markdown 子集 → React
  节点（零 innerHTML）；链接强制 `noopener noreferrer`；图片仅接受
  `http(s)` 外链（决策 7），`javascript:`/`data:` 一律不渲染为可点击。
- **分享**：`SHARE_KIND_PATHS` +`doc`，公开页为钉死版本的只读渲染
  （`ShareView` 增 `doc` 模式）；TTL 档与限流复用既有机器。
- **市场与物化显式排除**：`MARKETABLE_KINDS`/`MATERIALIZABLE_KINDS` 不加
  `doc`（决策 6）；产物详情页对 `doc` 隐藏可见性设置区（服务端 422 是兜底，
  UI 不给入口避免「看得见点不动」）。
- **展示面收录**：workbench 徽标/产物页/筛选/i18n zh+en（含「七类产物」文案
  改「八类」）；`web/src/console/testing.ts` 的空白创建 mock 白名单同步。

## Impact

- 代码（后端）：`artifacts/{models,service,schemas}.py`（kind 常量、空白
  spec、校验分派、草稿层读写路由）、`share/routes.py`（kind 白名单）、
  `workbench.py`（展示序）；**新增一张表** `artifact_drafts` → 需要 Alembic
  迁移（`Artifact.kind` 本身是无约束 String(64)，kind 新增不需要迁移，但草稿
  表需要）。
- 代码（前端）：`console/api.ts`（kind 枚举 + 客户端）、`ArtifactsPage.tsx`
  （徽标/行渲染/面板分派/隐藏可见性区）、新增 `console/doc/*`、
  `views/ShareView.tsx`（doc 模式）、`i18n/{zh,en}.ts`（kind 标签、编辑器
  文案、创建弹窗块）、`testing.ts`。
- **依赖**：前端 **零新增依赖**（决策 10 的直接结果）。
- 数据：新表 + 新 kind 行；既有产物零迁移、零行为漂移（探索确认
  `ARTIFACT_KINDS`/AI 生成门/AI 工具面都不动）。
- 文件服务联动：**本变更不接** `fd-file-service` 上传（决策 7）；仅在文档
  里写明插图 v1 = 外链 URL、v2 = 文件服务上传通道（owner=`wire:<customer_id>`，
  接口形状已就绪）。
- 合规：文档正文与既有产物同等域隔离（跨企 404 不泄漏存在性）；分享页
  noindex（对齐 dashboard 分享页既有语义）。

## Capabilities

### New Capabilities

- `rich-text-docs`: doc 产物的正文契约（Markdown 源文本）、草稿层语义
  （自动保存与版本链分离）、渲染安全底线（结构上不可注入）、编辑器与分享面。

### Modified Capabilities

- `artifact-lifecycle`：空白创建白名单 +doc；产物正文（doc kind）的存续契约。
- `dashboard-share`：分享 kind 白名单 +doc（TTL/钉版/撤销语义复用）。

## 待评审确认

- 重建项 9-12 是否与 grilling 原意一致（尤其 **10 零依赖自研渲染器** 与
  **12 沿用域级权限**——若原意是引入 sanitizer 库或文档级 ACL，请指出）。
- 正文上限 256 KB 是否合适（可调配置）。
- 文档是否需要「导出」（md/docx/pdf 下载）——本轮未定，默认 v1 不做。
