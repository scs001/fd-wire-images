# refresh-scheduler Specification

## Purpose
定义统一刷新调度器：派生数据集与看板产物的定时刷新（计量驱动同款后台 tick
范式）、间隔地板与客户配额、级联刷新（父刷新传导派生子、防环）、目标失存自动
清理——把「持续迭代」从手动按钮升级为自动化。

## Requirements

### Requirement: 调度表与 CRUD

登录客户可为其属主目标创建刷新调度：派生数据集（derived 来源）与 dashboard
产物两类；间隔有地板（dataset ≥60 分钟、artifact ≥360 分钟——AI 刷新耗
token，地板封无人值守成本）；每客户配额（默认 10，env 可调）；列表/删除/
暂停/恢复；跨客户 404。

#### Scenario: 建调度地板
- **WHEN** 客户试图为看板创建 60 分钟间隔的调度
- **THEN** 被拒并提示最小间隔 360 分钟

#### Scenario: 类型校验
- **WHEN** 对非 derived 数据集或非 dashboard 产物建调度
- **THEN** 被拒并说明可调度目标类型

### Requirement: tick 执行循环

调度器以 lifespan 后台任务周期执行（enabled/tick 间隔 env 可控）：到点且未
暂停的调度逐个执行（全局串行，同步执行体经线程包裹）；执行前置 running、
后置 ok/failed（含 last_error）并推进 next_run_at；目标已删除或归档时自动
移除调度行；单轮异常记录后继续不断环。

#### Scenario: 到点执行
- **WHEN** 调度到点且未暂停
- **THEN** 执行刷新，状态推进为 ok 或 failed（含原因），next_run_at 前移

#### Scenario: 目标失存清理
- **WHEN** 调度目标的数据集/产物已被删除或归档
- **THEN** 调度行被自动移除，不再空转

### Requirement: 级联刷新

数据集刷新支持级联：刷新完成后其 derive 来源链上的子数据集依次传递刷新
（transitively、visited 防环、深度上限）；手动刷新端点提供 cascade 开关
（默认开）；级联中任一环失败记录该环错误但继续其余环。

#### Scenario: 三层链传导
- **WHEN** 顶层派生数据集刷新且 cascade 开启
- **THEN** 其二级、三级派生子依次刷新完成

#### Scenario: 环防护
- **WHEN** 派生链出现环（异常数据态）
- **THEN** 已访问集合阻断重复刷新，不无限递归
