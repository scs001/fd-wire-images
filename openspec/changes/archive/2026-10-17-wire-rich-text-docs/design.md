# Design: wire-rich-text-docs

## Context

- 全仓零富文本/markdown 依赖（`web/package.json` 仅 6 个运行时依赖）；
  `dangerouslySetInnerHTML`/`innerHTML` 在 `web/src` 与 `api/fd_wire_api` 零命中。
- 现有产物编辑器是裸 JSON textarea（`ArtifactsPage.tsx:1201-1402` 的 `SpecViewer`）。
- `Artifact.kind` 是无约束 `String(64)`（`artifacts/models.py:62`）——新增 kind
  不需要迁移；新增**表**才需要（草稿层落这张表）。
- 分享机器就绪（HMAC 令牌 / 钉版 / TTL / 归档即失效，`share/service.py`）；
  市场与物化各有 kind 白名单（`market/artifact_service.py:53`、
  `materialization/specs.py:42-44`），不加即排除。
- 平台文件服务 `fd-file-service` 已建成（owner 域 + 预签直传 + 时效取用），
  但本变更**不接入**（决策 7：外链图片优先）。

## Goals / Non-Goals

**Goals**：doc kind 全链（空白创建 → 编辑器 → 草稿层/版本链 → 发布 → 分享）；
渲染安全结构上不可注入；零新增前端依赖。

**Non-Goals**：文件服务上传通道（v2）；导出 md/docx/pdf（待评审）；协同编辑
（多人同编）；评论/批注；文档进市场/物化（显式排除，决策 6）。

## Decisions

### D1: 正文 = Markdown 源文本（不存 HTML）

**选择**：`spec = {format:"markdown", text:"<md 源>"}`；不存预渲染 HTML。

**理由**：决策 3（md 分栏）已把 md 定为编辑形态，md 即事实源；存 HTML 会
(a) 锁死渲染器（改样式要重渲全部存量）、(b) 把 XSS 面从「渲染器一处」扩散
到「存库数据一片」——消毒必须在下游每个消费点重复。而存 md 时，注入面收敛
到唯一渲染器（D2）。

**被否**：Tiptap/Slate 的 JSON document model（引入重依赖 + 自定格式，且
JSON→HTML 渲染仍要消毒，没省事反多一层）。

### D2: 零依赖自研最小 markdown 渲染器，输出 React 节点

**选择**：`web/src/console/doc/md.tsx` 实现最小子集——标题、粗斜、行内码、
代码块、无序/有序列表、引用、链接、图片、段落；**输出 React 元素**，不用
`dangerouslySetInnerHTML`；原始 HTML 标签按纯文本转义呈现。

**理由**（决策 8 的最强形态）：不产生 innerHTML = 结构上无可注入点，XSS 从
「事后消毒」升为「不可达」。React 文本子节点自动转义，无需 sanitizer 库。
同时维持零依赖（与仓库现状一致）。

**代价与边界**：markdown 子集外的语法（表格、脚注、内嵌 HTML）不渲染——
如实按纯文本呈现，不静默吃掉。若将来要完整 markdown，换渲染器即可（渲染
接口不变），但安全底线（无 innerHTML）必须保持。

**被否**：`marked`/`markdown-it` + DOMPurify——引入 2 个依赖 + 一条消毒
链，且消毒配置漂移是常见事故源；收益（语法覆盖）对 v1 报告文档不紧迫。

### D3: 草稿层独立表（不进版本链）

**选择**：`artifact_drafts(artifact_id PK/unique, text, updated_at, updated_by)`；
自动保存 = `UPSERT` 这行；「保存版本」= 把该行内容写入 `artifact_versions`。

**理由**（决策 4）：版本链是 append-only 的对外契约（分享钉版、市场快照、
物化引用都依赖它）；自动保存若进版本链，敲字期间会产生几百个版本，把历史
炸掉、把分享钉版语义搅浑。分离后：草稿层是「工作区」，版本链是「里程碑」。
草稿层一对一连产物（唯一约束而非多行历史——工作区不需要历史）。

**读路径**：编辑器打开时若草稿层存在则展示草稿层（未保存的正文不丢）；
无草稿层则展示当前版本 spec 的 text。**分享与版本历史只读版本链**（草稿层
永不参与——D3 的硬约束）。

**表而非列**：草稿层放 `artifacts` 表加列也能跑，但产物表是全系统最热的
表（列表/工作台/市场都扫），为只有一个 kind 需要的功能给全局表加列是污染；
独立表 + 外键级联删除，热点路径零变化。

### D4: 权限与归属沿用 Artifact 现有模型

**选择**：doc 继承 `customer_id` 域隔离 + console 角色门（viewer 只读、
editor/owner 可写），不新增 per-artifact ACL。

**理由**（推导项 12）：全仓无 per-artifact ACL（探索确认），record-view 先例
即「镜像 Artifact 不发明新模型」。文档与看板同权是当下最短一致路径；文档级
精细权限若客户要求，另立 change（会产生跨产物的权限模型，不该在本文档类
里局部发明）。

### D5: 分享走既有机器，市场/物化显式排除

**选择**：`SHARE_KIND_PATHS` +`"doc"`（路径 `/api/v1/share/doc/{id}`），
复用 HMAC/钉版/TTL/限流；`MARKETABLE_KINDS`、`MATERIALIZABLE_KINDS` 不加
`doc`（决策 6）。

**理由**：分享是「把已验证的机器复用一次」；市场是「对外可复制分发」——
文档正文含客户业务叙述，进市场需要额外的敏感度评审与版权语义（看板规格
尚可，叙述性文本不同），v1 明确排除。**UI 配合**：产物详情页对 doc 隐藏
可见性设置区（服务端 422 是兜底；不给入口避免「看得见点不动」的假 UI）。

### D6: 图片 = 外链 URL（v1）

**选择**：`![alt](https://...)` 渲染为 `<img>`；仅接受 `http(s)`；
非 http(s) 源不渲染为图片（安全底线的一部分）。文件服务上传通道留 v2。

**理由**（决策 7）：文件服务刚建成、云侧还没部署（任务 7.1 待执行）；
外链先行让文档能力今天就可交付。v2 接入形状已探明：owner=`wire:<customer_id>`、
预签直传、取用走时效地址——本变更在 README/文档里写明接口形状，不写代码。

## Risks / Trade-offs

- [自研渲染器语法覆盖有限] → 明确最小子集，子集外按纯文本如实呈现（不静默
  吃字符）；渲染器是纯函数（md → 节点），未来替换成本低。
- [正文 256KB 上限] → 对「报告/说明书」量级充裕（256KB ≈ 十几万字）；上限
  可配置（`FD_WIRE_DOC_MAX_BYTES`），超限 422 载明。
- [草稿层与版本链双写不一致窗口] → 自动保存与保存版本各自独立事务；最坏
  情形=草稿比最新版本新（预期语义），不存在数据丢失路径。
- [doc 分享页渲染性能] → 分享页渲染 md 为纯前端计算，256KB 上限内无风险；
  限流复用 share 机器。
- [i18n 文案「七类产物」需改八类] → 已知触点清单在 tasks 里逐项列出
  （zh.ts:620 / en.ts:662 及示例计数）。
