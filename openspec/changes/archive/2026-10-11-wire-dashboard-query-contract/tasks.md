# Tasks: wire-dashboard-query-contract

## 1. api 契约模块

- [x] 1.1 `api/fd_wire_api/artifacts/query_contract.py`：`parse_panel_query`
  （宽容解析/归一：dataset_id 非空、filters 算子白名单、sort dir 白名单、
  limit 夹取 1..1000、columns 字符串列表；非法整体返回 None）+
  `contract_panels(spec)` 提取 + `refresh_spec_with_contracts()` 确定性执行
  （api tier → `api_store.execute_query`；cell tier → EngineClientFactory
  TOOL_QUERY；快照归一：columns 投影列序、值 str/number/null、bool 降格
  字符串不丢数据；面板级失败隔离 + 全失败抛 RefreshFailed；返回
  (new_spec, per-panel results)）
- [x] 1.2 `artifacts/schemas.py`：`ArtifactRefreshOut` 扩展 `mode` 与
  `panels`（可选字段，向后兼容）；`artifacts/routes.py` refresh 端点接
  决策分支（≥1 合法 query → 契约路径零 LLM；0 个 → AI 轮原路径），单飞锁
  两路共用，契约路径全失败 502 零半写
- [x] 1.3 `ai/tools.py`：`dashboard_spec_create` 指令升级——数据集查询
  取数的面板必须落 query 引用（dataset_id + 查询形参），kpi 用
  sort+limit1+columns 投影约定，source 仍保留
- [x] 1.4 测试：query_contract 单测（解析边界/归一/投影/类型降格）+
  端点测试（契约刷新新版本/混合 kept/全失败 502/跨客户数据集面板级失败/
  免费档零引擎零 LLM/旧看板 AI 路径不回归/单飞共用）

## 2. web 契约面

- [x] 2.1 `console/dashboard/spec.ts`：`DashboardPanelQuery` 类型 +
  `normalizePanelQuery` 宽容归一（与服务端同界：算子/方向/limit/列序），
  parse 时挂到面板、非法丢弃；编辑器 helpers（duplicate/convert）保 query
- [x] 2.2 `DashboardEditor.tsx`：面板详情区「已绑定查询」徽标（dataset_id
  展示）；编辑动作不抹 query
- [x] 2.3 `DashboardPane.tsx`：刷新响应按面板呈现（成功/保留/失败计数与
  失败原因列表）；`console/api.ts` 刷新响应类型扩展（mode/panels）
- [x] 2.4 测试：spec.ts query 归一单测（合法/非法/投影保序）、编辑保留
  query、刷新结果面板渲染（含失败原因）、快照渲染不读 query

## 3. 收口

- [x] 3.1 api 全量 pytest + web vitest + tsc 全绿
- [x] 3.2 生产发布（镜像滚版 + manifest + ArgoCD）与实弹验证（契约看板
  刷新零 LLM、旧看板 AI 路径不回归）
