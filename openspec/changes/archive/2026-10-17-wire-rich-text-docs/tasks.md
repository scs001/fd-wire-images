# Tasks: wire-rich-text-docs

## 1. 后端：kind 与正文契约

- [x] 1.1 `artifacts/service.py` 增 `DOC_KIND = "doc"` 常量 + `_blank_spec` 分支（`{format:"markdown", text:""}`）；`validate_kind_spec` 增 doc 分派；验证：空白创建 doc 返回 201 且 spec 形状正确
- [x] 1.2 doc spec 校验：format 必须 `"markdown"`、text 必须字符串、正文 ≤ 上限（`FD_WIRE_DOC_MAX_BYTES`，缺省 262144）；违规 422 定位字段零半写；验证：构造缺 format/非 markdown/text 超限三例均 422 且版本数不变
- [x] 1.3 `BLANK_CREATE_KINDS` +`"doc"`（`service.py:545`）；验证：空白创建 doc 放行、未知 kind 仍 422
- [x] 1.4 显示面两处展示序同步：`workbench.py` 的 `WORKBENCH_KINDS` +`"doc"`；验证：工作台计数含 doc 且与 web 枚举一致

## 2. 后端：草稿层

- [x] 2.1 新表 `artifact_drafts`（`artifact_id` 唯一外键级联删、`text` Text、`updated_at`、`updated_by`）+ Alembic 迁移；验证：alembic upgrade/downgrade 双向通过，产物删除草稿行随删
- [x] 2.2 草稿读写端点：`GET/PUT /api/v1/artifacts/{id}/draft`（仅 doc kind；PUT 覆盖写不产生版本行；archived 拒绝写 403；跨企 404）；限长复用 1.2 的上限校验；验证：连续 PUT 三次版本链零新增、GET 取回最新、跨企 404
- [x] 2.3 编辑器打开读路径：优先草稿层、无则当前版本 spec.text（由前端两请求组合或端点返回，择一实现并在 tasks 记明）；验证：自动保存后刷新页面正文不丢

## 3. 前端：渲染器（安全底线）

- [x] 3.1 `web/src/console/doc/md.tsx`：最小 markdown 子集 → React 节点（标题/粗斜/行内码/代码块/列表/引用/链接/图片/段落）；**零 innerHTML**；验证：单测断言 `<script>`、`onerror=`、原始 HTML 标签均不出现在输出 DOM（仅文本）
- [x] 3.2 链接与图片源协议约束：链接 `noopener noreferrer`；图片仅 `http(s)`（`javascript:`/`data:` 不渲染为可加载源）；验证：单测覆盖 `javascript:` 链接与 `data:` 图片两例
- [x] 3.3 子集外语法如实降级（按纯文本呈现，不静默吞字符）；验证：表格/脚注样例渲染为文本且字符不丢

## 4. 前端：编辑器与产物面

- [x] 4.1 `web/src/console/doc/DocEditor.tsx`：Markdown textarea + 实时预览分栏；自动保存（debounce）落草稿层；保存状态如实呈现（未保存/保存中/已保存/失败）；「保存版本」按钮；archived 只读；验证：真对话/真点击走一遍保存状态四态
- [x] 4.2 `ArtifactsPage.tsx` doc 分派：行渲染、展开面板挂 DocEditor（对齐 `DashboardPane` 等既有模式）、徽标颜色、版本抽屉复用；验证：doc 行展开即编辑器
- [x] 4.3 doc 隐藏可见性设置区（不进市场，D5）；验证：published doc 详情页无可见性区，看板仍有
- [x] 4.4 `console/api.ts`：`ARTIFACT_KINDS` +`"doc"`、`ArtifactKind` 类型、`createBlankDoc` 客户端、草稿读写客户端、`createDocShareLink`；验证：typecheck 通过 + 各客户端有调用点
- [x] 4.5 空白创建弹窗：`createDoc` i18n 块（按钮/标题/占位/提交/取消/必填/失败）；验证：控制台可建空文档并进入编辑器

## 5. 分享面

- [x] 5.1 `share/routes.py`：`SHARE_KIND_PATHS` +`"doc"`、公开端点 `GET /api/v1/share/doc/{id}`（令牌验签 + 钉死版本 + 仅返回该版本正文 spec）；验证：生成链接未登录打开呈现正文；draft/archived 404
- [x] 5.2 `ShareView.tsx` doc 模式：复用渲染器（安全底线同适用）、noindex、品牌署名；验证：分享页无编辑入口、响应含 noindex
- [x] 5.3 归档即失效与改版不串版复用既有语义；验证：归档后旧链接 404；再存新版后旧链接仍呈现钉死版本

## 6. 展示面与文案

- [x] 6.1 i18n zh/en：`console.artifacts.kinds.doc` 标签、编辑器文案块、创建弹窗块、「七类产物」→「八类」计数文案（zh.ts:620 / en.ts:662）；验证：`i18nKeys.test.ts` 通过（zh/en 键位一致）
- [x] 6.2 `web/src/console/testing.ts` 空白创建 mock 白名单 +doc 分支；验证：console 测试通过
- [x] 6.3 文档：README 或 docs/ 增「富文本文档」节（含插图 v1=外链、v2=文件服务接口形状、正文上限）；验证：文案与实现一致（复核）

## 7. 验收

- [x] 7.1 端到端（真对话/真点击）：建空文档 → 写作（含标题/列表/代码/外链图片）→ 自动保存 → 刷新不丢 → 保存两个版本 → 归档 → 恢复 → 发布 → 生成分享链接 → 未登录打开呈现钉死版本 → 归档分享失效；验证：逐步骤留证据（截图/响应）
- [x] 7.2 XSS 底线实测：正文注入 `<script>`/`onerror=`/`javascript:` 链接/`data:` 图片四例，预览与分享页均不执行；验证：浏览器 DevTools 断言无脚本执行、无 DOM 注入
- [x] 7.3 存量零漂移：回归既有七类产物的保存/分享/市场路径不受影响；验证：既有测试全绿 + 抽一条看板分享链接实测
- [x] 7.4 正文上限与格式校验实测：超限 422、非 markdown 422；验证：响应含明确错误与定位
