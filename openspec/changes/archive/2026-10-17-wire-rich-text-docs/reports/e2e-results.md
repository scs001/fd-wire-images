# E2E 验收记录（wire-rich-text-docs 任务 7.1-7.4，2026-10-17）

三层证据，按「离产品代码的距离」从近到远：

1. **真 HTTP 端到端**（`api/scripts/e2e_doc.py`，31/31）：真 uvicorn 进程 + 真
   迁移（`alembic upgrade head` 至 0038）+ 真 HTTP 栈——语义层最强证据，可
   在 CI/本机一键重跑。
2. **真浏览器手验**（本轮，浏览器 DevTools 断言）：console 走**真会话建立**
   （假 JWKS 但真签名 token，`establishSession` 真跑）+ 公开分享页无鉴权真
   打开——7.1 全链与 7.2 四例攻击串的实际执行证据。
3. **全量套件**（api 1320 通过 / web 847 通过）：存量零漂移的广度证据。

## 0. 过程中抓出并修掉的一个真缺陷（7.2 浏览器实测的副产品）

**intraword 下划线强调**（`web/src/console/doc/md.tsx`）：`INLINE_PATTERN`
的 `_[^_]+_` 不设词边界，`window.__XSS_EXECUTED=1` 被渲染成
`window._<em>XSS</em>EXECUTED=1`——真实文档里的 snake_case/dunder 标识符
（`user_id`、`__init__`）会被斜体化并吞掉一个下划线。CommonMark 规定 `_`
强调不得 intraword（`*` 可以）。已改为前后断言 `(?<![\w])…(?![\w])` 并补
`__强__` 强分支；新增 2 条回归测试（md.test.tsx，14/14）。修复后浏览器
复核：`window.__XSS_EXECUTED=1` 原样呈现、零 `<em>`。

## 7.1 端到端（真浏览器，全链）

环境：`api/scripts/e2e_doc.py --serve`（真迁移 + 假 JWKS 真签名 token + 种子
客户 `doc-e2e-browser`）起后端，`vite` 代理 `/api` 起前端；会话经
`establishSession` 真建立（非绕过）。

| 步骤 | 结果 |
|---|---|
| 建空文档（新建设单填写标题 → POST 201） | ✅ 进分栏编辑器，空正文 |
| 写作（标题/加粗/列表/围栏代码/外链图片/外链） | ✅ 真键盘逐段键入 |
| 自动保存（~1.2s debounce → PUT /draft） | ✅ 状态「已保存（19:13）」 |
| 刷新不丢（reload 后重开编辑器） | ✅ 草稿层恢复 190 字全文 |
| 实时预览渲染 | ✅ h1/li×2/pre/外链 img/外链 a 全出 |
| 保存版本 → v2 | ✅ 版本链 v2；**草稿层不清**（D3 硬约束实测成立） |
| 发布 | ✅ 状态「已发布」，出「生成分享链接」按钮 |
| 生成分享链接 | ✅ 弹层：TTL 四档/公开链接/到期时间/「改版后重新生成」提示；链接钉 v2 |
| 未登录打开（新标签，sessionStorage 为空） | ✅ 呈现钉死 v2 全要素 |
| 归档 | ✅ 状态「已归档」 |
| 归档后分享链接 | ✅ 「链接无效或已过期」统一态（不泄露原因） |
| 恢复 | ✅ 回到「草稿」态可继续编辑 |

说明：本机 IAB 宿主对 Playwright 的**指针事件注入失效**（count/isVisible 正常、
真键盘输入正常、合成 `el.click()`/`form.requestSubmit()` 正常——已定位为宿主
输入通道问题，非产品缺陷）；手验用「原生浏览器语义入口（requestSubmit/原生
click）+ 真键盘键入」完成，所有断言均来自真实 DOM/网络状态。证据即上表。

## 7.2 XSS 底线（四例攻击串，预览与分享页双侧）

攻击串（一次注入，两个面同时验）：
`<script>…</script>` / `<img src=x onerror="…">` / `[点](javascript:…)` /
`![图](data:text/html;base64,…)`，另用 `window.__XSS_EXECUTED` /
`window.__EDXSS` 作执行哨兵。

| 断言 | 分享页 | 编辑器预览 |
|---|---|---|
| 哨兵变量未被置位（无脚本执行） | ✅ null | ✅ null |
| `<script>` 不成为 DOM 元素（`script` 计数） | ✅ 0 | ✅ 0 |
| `<img onerror>` 不成为 DOM 元素 | ✅ 0（源文本可见） | ✅ 0 |
| `javascript:` 不成为可点击 href | ✅ 0 | ✅ 0 |
| `data:` 图片不渲染为 `<img>` | ✅ 0（alt 文本呈现） | ✅ 0 |
| 合法 https 外链/图片照常 | ✅ link+img 各 1 | ✅ img 1、外链 1 |
| `window.__XSS_EXECUTED=1` 标识符原样（修复后） | ✅ 逐字 | ✅ 逐字（user_id 同） |

服务端侧：正文**原样存取**（e2e_doc.py 5.x 两条）——存的是 Markdown 源文本
不是 HTML，服务端零渲染=零注入面。

## 7.3 存量零漂移

- api 全量 1320 通过 / 11 skipped；web 全量 847 通过（56 文件）。
- 真 HTTP 抽验（e2e_doc.py 6.x）：看板空白创建 spec 不变、发布、分享链接
  路径 `/share/dashboard/`、公开页 200——四条全绿。
- 迁移链：`alembic upgrade head` 0037→0038 在真库从零跑通。

## 7.4 正文上限与格式校验（真 HTTP 响应形态）

| 用例 | 响应 |
|---|---|
| `format: "html"` | 422 `spec.spec.format 必须是 "markdown"` |
| `text: 123` | 422 `spec.spec.text 必须是字符串（Markdown 源文本）` |
| 两次非法保存后 | 版本数未增（零半写，versions=1） |
| 草稿超上限（262144 字节） | 422 `正文超过上限 262144 字节（Markdown 源文本按 UTF-8 计）` |
| doc 设 market 可见性 | 422 `产物类型 'doc' 不支持市场可见性（允许: dashboard, research_report）`（D5 显式排除点名） |

## 跑法

```bash
# 1) 真 HTTP 端到端（31 项断言；自建临时库、自清理）
cd fd-wire/api && .venv/bin/python scripts/e2e_doc.py

# 2) 浏览器手验环境（另开终端 `cd web && npx vite --port 5199`，
#    按脚本输出的 sessionStorage 注入行建立会话）
cd fd-wire/api && .venv/bin/python scripts/e2e_doc.py --serve

# 3) 全量套件
cd fd-wire/api && .venv/bin/python -m pytest tests/ -q
cd fd-wire/web && npx vitest run
```

## 8. 部署态复验（2026-10-17 生产，wire.finddatatech.cloud）

出片 `sha-92b2409`（源真 sha `759cf50`，testaction002 build-pool 快照）滚版前
**迁移 0038 已先行执行**（生产 PG `wire_platform`，`alembic_version=0038_artifact_drafts`，
`artifact_drafts` 表/唯一索引/FK CASCADE 三件齐）；两 Deployment 滚起后以
**platform-test 账号真登录**（真 Logto 流程）复walk：

| 步骤 | 结果 |
|---|---|
| 生产控制台出「新建文档」+ 八类类型筛选 | ✅ |
| 建文档（新建设单，标题「生产验收 · 库区水位月报」） | ✅ 直进分栏编辑器 |
| 写作（标题/加粗/列表/围栏代码/外链）→ 自动保存 | ✅ 状态「已保存（19:36）」 |
| 刷新不丢（reload 后重开编辑器） | ✅ 草稿 114 字全文恢复 |
| 保存版本 v2 → 发布 → 生成分享链接 | ✅ 弹层出完整链接（钉 v2、30 天档） |
| **未登录新标签打开分享链接** | ✅ 标题/H1/列表×2/代码块/外链（`noopener noreferrer`）全出，`<script>` 计数 0、无登录会话 |
| 归档 → 旧链接 | ✅ 「链接无效或已过期」统一态 |
| 恢复 | ✅ 回「草稿」态可继续编辑 |
| 收尾 | 测试文档归档保留（demo 账号列表整洁；未做任何删除） |

## 遗留

- [x] 部署态复验：见 §8（生产真登录全链通过）。
- [ ] IAB 宿主指针注入失效是本机工具链问题（非产品）：本轮手验以「原生浏览器
      语义入口（requestSubmit/原生 click）+ 真键盘键入」完成，所有断言均取自
      真实 DOM/网络状态。如后续用 Playwright headless 复跑可全链鼠标化。
- [ ] 分享页浏览器面（`#/share/doc`）的移动端宽度适配未单独走查（与既有
      分享页同款响应式容器，风险低）。
