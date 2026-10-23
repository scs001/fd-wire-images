# Proposal: wire-dataset-form-editing — 数据集表单编辑（api 档本地修正层 + 双档统一编辑器）

## Why

方向 B 第二支柱（用户定调）：「可修改的表单页——客户在数据集上记录/处理数据」。
现状两断：

1. **cell 档后端已齐**：修正三件套（update/delete/insert 覆盖层 + 回滚 + 悬空
   如实 + actor 审计）经控制台端点可用，云端永不写回客户源；
2. **api 档（免费档）完全不可编辑**：corrections 端点对 api 层数据集 403 档位
   闸（实现细节，非 spec 红线——free-tier-data 付费面清单未含修正层）；
   免费档数据集（上传/录入）恰恰是轻量记录场景的主场；
3. **前端无编辑面**：DatasetDetail 只有只读行预览，客户看到错值只能重传。

## What Changes

- **api 档本地修正层**（镜像引擎覆盖层语义）：
  - 新表 `dataset_corrections`（datasets 库，create_all 建）：op
    （insert/update/delete）、row_anchor（基表 row_index）、column、
    value_json、actor、created_at、reverted_at；
  - 三个控制台端点对 api 层放行本地实现（响应契约与 cell 档不变）：
    POST corrections（列名校验：未知列 400 如实）、GET corrections（含
    已回滚/悬空标注）、POST revert；
  - **有效视图统一**：本地读取一口径合并覆盖层——preview_rows（控制台）
    与 data 面 query/aggregate（wdk 轨道）都呈现修正后视图，杜绝控制台
    与数据面两头看到的Data 不一致；零修正数据集走原 SQL 快路径（零回归）；
  - 写后同步 dataset.row_count（有效行数）；insert 受免费档行数闸（超额
    如实拒绝，不产生部分执行）。
- **双档统一行内编辑器（web）**：DatasetDetail 行预览加「编辑」模式——
  单元格双击改值（update 队列）、行删除按钮（delete 队列）、「添加行」
  内联表单（insert 队列）；脏队列计数与未保存提示；保存 = 顺序提交
  corrections（既有 addCorrection 客户端）；失败逐条如实呈现（行上下文）；
  默认只读不动；两档 UI 完全一致（端点已统一）。

## Impact

- **specs**：新能力 `dataset-form-editing`（本地修正层 + 统一编辑器两要求）。
  free-tier-data 不动（付费面清单未含修正层，无口径冲突）。
- **api**：datasets/models.py 新表、api_store.py 覆盖层（apply/list/revert +
  有效视图合并进 preview/query/aggregate）、routes.py 三端点分支改造。
- **web**：DatasetDetail 编辑模式（+i18n 双语）。
- **兼容**：cell 档行为零漂移；api 档从 403 变可编辑（能力新增，不破任何
  既有测试断言之外的红线）；零修正数据集读取路径零改动。
- **风险**：覆盖层合并进 execute_query/aggregate 的语义漂移（对策：仅当
  数据集存在未回滚修正时切换 Python 合并路径，全量既有查询测试必须原样
  绿）；悬空修正（基行被并发删）如实标注不静默。

## Non-Goals

- 修正层的版本化/分支（v0 线性追加+回滚，同引擎语义）。
- cell 档引擎侧任何改动（引擎覆盖层不动，只做 api 档镜像）。
- 批量写（rows:batchWrite）对 api 档开放（仍付费面——修正是轻量人工编辑，
  批量写是程序通道，边界不混）。
- 协同编辑/冲突合并（单人编辑假设，v0 如实）。
