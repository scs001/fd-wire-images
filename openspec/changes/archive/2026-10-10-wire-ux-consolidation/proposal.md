## Why

柏讯门面上线后四处体验债集中暴露：控制台各状态卡贴左不居中；页脚与官网口径脱节（无备案信息、无跨服务入口，console/admin 也渲染同一全量页脚）；控制台 AI 对话因生产 secret 指错 token 网关 404 全不可用；需求单沿用八态"人肉报价交付"状态机，与实际生产方式（新增源很简单、panel 直接构建生成爬虫）不符。趁客户旅程尚未放量，一次收敛门面与旅程，并为将来"客户对话生成爬虫"的萬星闭环预留接口形态。

## What Changes

- **居中修复**：控制台五张状态卡（登录门/blocked/configError/sessionError/占位卡）补 `mx-auto`，对齐 DemandNewView 既有写法。
- **页脚统一**：门户页（首页/定价/文档/指标目录）换全量页脚=官网同款元素（组织名+GitHub+粤ICP备2026118740号-1+粤公网安备44030002016558号）+一行跨服务入口（官网/壹座 platform/识律 lex/更多产品线→官网）；console 用紧凑一行页脚（版权+ICP+公安备）；admin 无页脚。
- **AI 构建入口换芯（过渡皮）**：chat tab 改名"AI 构建"，内容替换为壹座试用引导卡（主按钮→demo 页带公示测试号导览，次级→platform 直达；卡片不复述测试号明文）；控制台默认 tab 从 chat 切"我的数据"；后端 `fd_wire_api/ai/` 对话循环保留、不加内部 UI（作为将来萬星对话的骨架种子）；修复生产 secret `token_gateway_url` → `https://token.finddatatech.cloud`。
- **需求单三态化（合并模型定调）**：客户面从占位转正为轻量意向单（需求描述自由文本必填+邮箱必填+公司名选填+目录页锚点预填）；状态机从八态压缩为 `submitted → in_progress → delivered`（+`rejected` 带理由），废除 evaluating/quoted/awaiting_confirm/building/delivery_acceptance，前后端同步；提交后文案承诺 1 个工作日响应；新单邮件通知 `wire@finddatatech.cloud`；console"我的需求"只读列表（三态徽章+时间+摘要）；定价页"自定义"卡主 CTA 改跳需求单（mailto 降次级）；存量非终态单粗归并至 in_progress（events 留痕）。

## Capabilities

### New Capabilities

- `portal-shell`: 门面壳层统一——页脚三形态（门户全量/console 紧凑/admin 无）、备案与跨服务入口、控制台状态卡水平居中。
- `console-ai-entry`: 控制台 AI 构建入口——壹座试用引导卡、默认落地"我的数据"、后端 AI 循环保留可用（token 网关修复）。
- `demand-intake`: 需求单收集与三态流转——轻量表单、三态状态机、提交通知、客户侧只读列表、定价页入口、存量迁移。

### Modified Capabilities

（无——现有八个 spec 的 requirement 均不受影响；console-auth 中"映射客户定向"场景语义不变，portal-pricing 只规范档位模型不涉及页面 CTA。）

## Impact

- **web/**：`App.tsx`（页脚分层）、`ConsoleView.tsx`/`ConsoleLayout.tsx`（居中、tab 命名）、`ChatPage.tsx`→壹座引导卡、`DemandNewView.tsx` 转正、`PricingView.tsx` CTA、`admin/demandsMachine.ts` 三态镜像、`i18n/zh.ts`+`en.ts` 全量文案。
- **api/**：`fd_wire_api/demands/statemachine.py` 白名单压缩与迁移脚本、新单邮件通知（wire@finddatatech.cloud）。
- **部署**：生产 secret `fd-wire-env` 的 `token_gateway_url` 修正 + api/web 双镜像滚动；demands 库存量数据迁移。
- **明确不做**（将来另立项）：panel 对企业客户开放、萬星 agent 实际接线、内部 AI 调试界面。
