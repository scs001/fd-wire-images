# Tasks — wire-assistant-panel

## 1. 后端问答轨

- [x] 1.1 `ai/answer.py` 新增 `answer_question()`：纯问答，`tools=[]`，不碰 ToolExecutor / cell 解析
- [x] 1.2 问答轨 system prompt 独立：帮助/指路口径，明确不承诺执行动作
- [x] 1.3 上下文块组装 `assistant_context_block()`：只读白名单键，产出标识+元数据文本
- [x] 1.4 上下文归属校验：`artifact_id` / `dataset_id` 非本企业即丢弃该项，不报错
- [x] 1.5 端点 `POST /api/v1/ai/answer`：会话归属校验 + 日限额 + 问答 + 落库
- [x] 1.6 后端测试：免费档可问答、上下文不含数据行、他企对象被丢弃、LLM 失败如实

## 2. 后端增量取消息

- [x] 2.1 `GET .../messages` 支持 `after_id` 查询参数（缺省=全量，语义不变）
- [x] 2.2 `service.list_messages` 增加 `after_id` 透传
- [x] 2.3 测试：after_id 只返回其后消息；非法 after_id 如实 422

## 3. 前端浮层骨架

- [x] 3.1 `web/src/console/assistant/AssistantPanel.tsx`：入口按钮 + 展开面板 + Esc 收起
- [x] 3.2 `AssistantProvider` 管理会话（单活会话、惰性创建、新对话）
- [x] 3.3 挂载点：`ConsoleView` ready 态包裹內容区
- [x] 3.4 公开面不渲染：分享页/橱窗页/未登录不入 provider

## 4. 前端问答与构建

- [x] 4.1 `api.ts` 新增 `askAssistant()` 与 `fetchMessagesAfter()`
- [x] 4.2 问答态：发问 → 等待 → 展示回答；失败保留输入可重试
- [x] 4.3 构建态：驱动既有 postMessage，期间按 after_id 轮询展示进展
- [x] 4.4 档位分轨：非免费档才渲染构建入口；免费档标出问答模式
- [x] 4.5 上下文采集：当前页签 / 当前产物 / 当前数据集
- [x] 4.6 去壹座深链（新标签页，不内嵌）

## 5. AI 构建页改版

- [x] 5.1 `ChatPage` 由引导卡改为浮层完整形态（复用同一套组件）
- [x] 5.2 保留壹座深链入口，移除纯跳转结构
- [x] 5.3 i18n：中英文案（浮层标题/占位/模式/失败/限额/去壹座）

## 6. 验证与收口

- [x] 6.1 前端测试：浮层开合、Esc、档位分轨、失败保留输入、公开面无浮层
- [x] 6.2 `cd api && python -m pytest` 全绿
- [x] 6.3 `cd web && npm test` 全绿
- [x] 6.4 类型检查 `tsc --noEmit` 与 `npm run build` 通过
- [x] 6.5 `openspec validate wire-assistant-panel --strict` 通过
- [x] 6.6 真机目视：控制台各页浮层开合与一轮真实问答
