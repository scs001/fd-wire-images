## 1. 门面壳层：页脚三形态 + 状态卡居中

- [x] 1.1 抽页脚常量与组件：全量页脚（组织+GitHub+ICP 粤ICP备2026118740号-1+公安备+服务行：官网/壹座 platform/识律 lex/更多产品线→官网）与紧凑页脚（版权+ICP+公安备）两组件，备案口径对表 fd-official-web Layout.astro。验证：组件单测断言备案号与服务入口链接。
- [x] 1.2 路由层分发：门户页（首页/定价/文档/指标目录）挂全量页脚，#/console/* 挂紧凑页脚，#/admin 无页脚。验证：四门户页+console+admin 六路由渲染快照/测试各一。
- [x] 1.3 居中修复：ConsoleView 四张状态卡（anonymous/blocked/configError/sessionError）与 ConsoleLayout 占位卡补水平居中，对齐 DemandNewView 既有写法。验证：现有居中测试范式补五例（含未登录 #/console 卡片居中）。
- [x] 1.4 i18n 全量补齐：zh/en 双语新增页脚、引导、需求单文案键。验证：i18n 键完整性测试（zh/en 键集一致）。

## 2. AI 构建入口：壹座引导 + 后端可用

- [x] 2.1 ChatPage 客户面替换为壹座试用引导卡：主按钮→官网 demo 页、次级→platform.finddatatech.cloud，卡片不复述测试账号明文。验证：组件测试断言两链接与"不含测试账号明文"。
- [x] 2.2 侧栏 tab 改名"AI 构建"、控制台默认 tab 切"我的数据"（#/console 无子路径落 datasets）。验证：路由测试默认落地 + 旧 chat 深链仍可达引导卡。
- [x] 2.3 修生产 secret fd-wire-env 的 token_gateway_url → https://token.finddatatech.cloud（不带 /v1），滚动 api。验证：持凭据实弹 POST /api/v1/ai 一轮会话，LLM 网关不再 404。

## 3. 需求单后端：三态压缩 + 通知 + 迁移

- [x] 3.1 statemachine.py 白名单压缩为 submitted→in_progress→delivered（+rejected 带理由），五个旧态不可迁入，非法迁移仍 409。验证：状态机单测覆盖合法迁移全集+旧态拒绝+rejected 必带理由。
- [x] 3.2 需求单数据模型与 API 校应收窄：表单字段（描述必填/邮箱必填/公司选填/目录锚点预填），旧态名不出现在任何响应。验证：API 测试三件（必填校验/锚点入参/响应无旧态）。
- [x] 3.3 新单邮件通知 wire@finddatatech.cloud：submitted 时异步发信，失败留痕不阻塞提交。验证：mock SMTP 单测两例（送达/失败不阻塞）。
- [x] 3.4 存量归并迁移脚本：五旧态→in_progress，事件留痕，幂等重跑无二次变更。验证：迁移测试（含 delivered/rejected 不动 + 幂等断言）。
- [x] 3.5 前端 demandsMachine.ts 镜像同步三态。验证：镜像与后端白名单一致性测试。

## 4. 需求单前端：表单转正 + 列表 + 定价入口

- [x] 4.1 DemandNewView 从占位转正：三项字段表单+必填校验+提交成功确认态（"已收到，1 个工作日内响应"）。验证：表单测试（空提交拒绝/成功确认文案）。
- [x] 4.2 目录详情页锚点预填链路：从 catalog 详情发起时描述区预填指标标识。验证：预填集成测试一例。
- [x] 4.3 console"我的需求"只读列表：三态徽章+提交时间+摘要+拒绝理由，无迁移操作控件，仅见本人单。验证：列表测试两例（展示/跨客户隔离）。
- [x] 4.4 定价页"自定义"卡主 CTA 改跳需求单、mailto 降次级。验证：PricingView 测试断言 CTA 行为变更。

## 5. 集成与发布

- [x] 5.1 全量回归：web vitest + api pytest 全绿，openspec validate wire-ux-consolidation --strict 通过。
- [x] 5.2 双镜像构建滚动（web+api 经既有发布通道），生产验证六路由页脚形态、控制台居中、AI 构建引导卡、需求单提交→通知→列表全链、存量迁移后 admin 派发台只见三态。
- [x] 5.3 运营知会：派发台八态→三态操作语义变化同步内部（备注进 change reports 或 INFRA-NOTES）。
