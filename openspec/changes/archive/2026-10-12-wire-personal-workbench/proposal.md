# wire-personal-workbench

## Why

方向 D（个人信息工作台）的近期清单只剩最后一件：**薄工作台 web（共享面+个人
产物区）**。其余件均已上产——免费档放行（wire-free-tier-data-gate）、源包订阅
视图（wire-source-packs）、产物转正（wire-artifacts-persistence）、看板全线、
偏好持久化（wire-personal-preferences）、分享（dashboard-share）。

现状问题：控制台九个页签（AI 构建/产物/我的数据/实体/源包/我的需求/用量/
数据面 Key/团队）**彼此平铺、无聚合主页**。个人用户登录后落在「我的数据」，
要自己拼出「我的源×我的看板×我的规则」的全貌——各页分别打开才知道有什么。
对 C 端起步（ROADMAP 方向 D「快速搭建」定调）这是上手摩擦：信息中枢的价值
在于**一屏看全自己的信息资产**。

同时本次清一笔滞后文案债：ROADMAP §3「已知的可见性三断点」与 §6 近期清单中
「可见性三断点补齐」「wire 专用 PG 起步」实已于 2026-10-11 全部闭环
（wire-free-tier-data-gate / wire-connector-distribution / wire-dedicated-postgres
凌晨生产搬迁 82ccf0c），部署清单中 datasets 库注释仍写 sqlite——一并修正为
已上产口径，防止后续会话被误导。

## What Changes

- **聚合端点 `GET /api/v1/workbench`**（登录客户身份，只读，免费档可用零计量）：
  单次往返返回六个 section——
  - `datasets`：总数、api/cell 分层计数、最近 ≤5 条（id/名称/存储层/行数/更新
    时间）、免费档真实配额（used/limit；付费档 null）；
  - `packs`：订阅数、最近订阅 ≤5 条（包 id/名称）；
  - `artifacts`：活跃产物总数（draft+published）、按类计数（六类展示口径，
    含表单构建器的 form——与产物页一致；校验面五类 tuple 不动）、最近 ≤5 条
    （id/kind/title/status/updated_at）；
  - `alerts`：alert_rule 产物计数、firing 中计数（alert_states）、24h 事件数
    （alert_events）；
  - `demands`：进行中计数（submitted+in_progress）、最新一单（code/status/
    updated_at）；
  - 响应仅含本客户数据（跨客户隔离，沿用既有 leak-guard 纪律）。
- **控制台「工作台」页签（首位）**：`#/console/workbench`——
  - 免费档：配额条内联（沿用 free-tier 语义：限额触达内联升档而非整页拦截）；
  - 各 section 卡片：计数 + 最近条目（深链既有页：数据集详情/源包页/产物页/
    需求页）+ 空态上手 CTA（上传 CSV/浏览源包/去 AI 构建）；
  - agents 入口紧凑卡：与「AI 构建」页壹座引导同语义链接（主入口官网 demo 页、
    次级壹座正式入口；卡片不复述测试账号明文——console-ai-entry 红线）。
- **落地页候选**：landing_tab 偏好值域新增 `workbench`；默认落地不变（我的数据，
  console-ai-entry 既有要求）；切页签即写语义不变（最后停留页=下次落地）。
- **滞后文案修正**（不改变行为）：ROADMAP §3 三断点段、§6 近期清单、
  `deploy/k8s/fd-wire-api.yaml` datasets 库注释改为已上产口径。

## Design Decisions

- **单聚合端点而非前端扇出**：六个 section 一次往返；三库（wire_platform/
  wire_datasets/wire_ai）同实例，各走既有模块会话依赖，无跨库事务需求（纯读）。
- **不新建 per-member 数据**：工作台是「客户面的个人视角聚合」，不落新表、零
  迁移；个人化（置顶看板/落地页）复用既有偏好件。C 端「个人产物区」本轮以
  客户面产物最近列表承接（产物模型无 per-member 属主列；团队空间下 viewer 也
  能看工作台，符合只读语义）。
- **默认落地不动**：console-ai-entry 明文冻结「裸进 #/console 落我的数据」；
  workbench 仅作为候选加入值域，避免动既有 spec 语义。

## Capabilities

### New Capabilities

- `personal-workbench`: 个人工作台——聚合端点（六 section 单往返、跨客户隔离、
  免费档可用）、控制台工作台页（计数+最近+空态 CTA+壹座入口）、落地页候选。

## Impact

- **api**：新增 `fd_wire_api/workbench.py`（router+service）；`app.py` 挂载；
  测试 `tests/test_workbench.py`（聚合正确性/跨客户隔离/免费配额/鉴权 401）。
- **web**：`console/WorkbenchPage.tsx`；ConsoleLayout 增 workbench 页签（首位）；
  ConsoleView 路由；i18n zh/en；测试。
- **docs/ROADMAP.md + deploy/k8s/fd-wire-api.yaml**：滞后文案修正。
- 零迁移、零新表、零 env 变更。
