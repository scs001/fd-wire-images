## Context

柏讯门户与控制台已由 wire-self-serve-launch / wire-customer-data-engine 等线交付在产：单页应用（#/ 门户 + #/console 控制台 + #/admin 内部后台）共用一个全量页脚；控制台各阻塞/引导态卡片布局类缺水平居中；对话构建走 `fd_wire_api/ai/`（conversations/messages/artifacts + tools/executor/loop），LLM 出口 `POST {TOKEN_GATEWAY_URL}/v1/chat/completions`，生产 secret 指错导致 404；需求单后端已有八态状态机（`demands/statemachine.py` 白名单 + 前端 `demandsMachine.ts` 镜像），客户面 `DemandNewView` 仍是占位。官网（fd-official-web）页脚携 ICP/公安备为备案口径唯一出处。

## Goals / Non-Goals

**Goals:**
- 门面（页脚/居中/备案/跨服务入口）一次对齐官网口径，按界面类型分三形态
- 客户旅程收敛：AI 试用指壹座、需求单走三态轻量轨道
- 为"客户对话直接生成爬虫"的萬星闭环保留两个种子：后端 AI 循环代码 + 需求单 in_progress 观测挂点，本 change 只换皮不动骨架

**Non-Goals:**
- panel 对企业客户开放（租户/权限隔离另立项）
- 萬星 agent 实际接线（生成流已有 source-generation-flow，接入是独立提案）
- 内部 AI 调试界面、对话数据的管理后台
- 需求单自动评估/自动报价

## Decisions

### D1 合并模型：需求单终态即对话（本 change 只落过渡形态）

需求单与对话页未来是同一入口：客户对话描述需求 → 萬星生成爬虫 → panel 自动定时爬取 → 交付物挂 cell。三态机是这条流水线的观测壳（对话开始=已收到、agent 生成/试跑=处理中、挂 cell=已交付）。因此：
- 表单描述字段为自由文本，按"未来第一条消息"设计，不引入结构化下拉；
- `in_progress` 文案不写死"人工处理"，将来可无缝换成 agent 进度；
- console"我的需求"列表行结构（时间+摘要+状态）在换芯时保持不变，行终态即对话入口。

**替代方案**：双轨（需求单走人工询价、对话页走自助生成并行）——被否：与"新增源很简单、panel 直接构建"的实际生产方式不符，且要维护两套流程语义。

### D2 页脚三形态由路由层分发，不做全局单例

门户视图（首页/定价/文档/指标目录）渲染全量页脚组件；`#/console/*` 渲染紧凑页脚；`#/admin` 不渲染页脚。备案号/服务入口等常量集中一处（i18n + 常量文件），与 fd-official-web 的口径对表维护。测试账号明文不进柏讯代码——demo 页为唯一出处。

### D3 对话后端保留原样，只修出口

`fd_wire_api/ai/` 模块代码不动、不加内部 UI；仅修生产 secret `token_gateway_url=https://token.finddatatech.cloud`（不带 `/v1`，客户端自行拼接）。客户面 `ChatPage` 整页替换为壹座引导卡；tab 改名"AI 构建"一次到位（终态是萬星对话，中途不再改名）；控制台默认 tab 切"我的数据"。存量会话数据不迁移不删除。

### D4 状态机压缩走"白名单收窄 + 一次性归并迁移"

服务端 `statemachine.py` 白名单压为三态+rejected（服务端仍为唯一裁决面，非法迁移 409 如实回显）；前端 `demandsMachine.ts` 镜像同步。存量迁移为幂等一次性脚本：五个废除态全部归并 `in_progress`，事件轨迹留痕；delivered/rejected 不动。报价信息若存在于旧单字段，随单保留可查，不字段级映射（占位期提交量极小，考古无价值）。

### D5 通知与提交解耦

新单 submitted 后异步发信至 wire@finddatatech.cloud；邮件失败仅留痕（日志/事件），绝不阻塞客户提交路径。发信通道复用现有邮件基础设施；无则最简 SMTP 直发，不引入队列。

## Risks / Trade-offs

- **1 个工作日是对外 SLA**：文案一旦上线即承诺，内部须有巡检/邮件通知兜底（D5 通知即为此服务）；兑现风险由业务节奏承担，超出再放宽文案。
- **八态砍三态是破坏性压缩**：admin 派发台操作语义变化（无 quoted/awaiting_confirm 可选），运营需知会；存量单归并后旧报价上下文只在事件轨迹里。
- **对话客户面下线后后端无消费者**：/v1/ai 端点在生产仅有内部调用可能，保留成本主要是维护心智；若萬星闭环立项时评估复用价值，届时再定去留。
- **壹座 demo 页依赖外部口径**：引导卡主入口依赖 finddatatech.cloud/demo 持续公示测试账号；demo 页下线则引导卡需换芯（链接常量化，改动面小）。
