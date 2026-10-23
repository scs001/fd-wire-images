# Tasks: wire-dataset-form-editing

## 1. api 本地修正层

- [x] 1.1 `datasets/models.py` 新表 `DatasetCorrection`（dataset_id/op/
  row_anchor/column_name/value_json/actor/created_at/reverted_at；datasets
  库 create_all 建，索引 dataset_id）+ `api_store.py` 三函数：
  apply_local_correction（校验列名/落行/行数闸/同步 row_count）、
  list_local_corrections（含 reverted/dangling 标注，形态对齐引擎面）、
  revert_local_correction（幂等拒绝二次回滚）
- [x] 1.2 有效视图统一：`_effective_rows` 合并助手（deletes 集合 + updates
  映射 + inserts 按序追加，插入行 `__row_id="c{correction_id}"`）；接入
  preview_rows；execute_query/execute_aggregate 在「存在未回滚修正」时切
  Python 合并路径（过滤/排序/limit 在合并结果上重算），零修正走原 SQL
  快路径
- [x] 1.3 `datasets/routes.py` 三端点分支：add_correction/list_corrections/
  revert_correction 对 api 层走本地实现（去掉该路径 require_cell_tier），
  响应契约与 cell 档一致
- [x] 1.4 测试：修正三操作+回滚+悬空、行数闸超额拒、有效视图两口径一致
  （预览 vs query vs aggregate）、零修正快路径零回归（全量既有
  query/aggregate 测试原样绿）、他企隔离、修正后 cascade/derive 相关
  api 层行为不破

## 2. web 行内编辑器

- [x] 2.1 DatasetDetail 行预览「编辑」模式：双击改值/行删除/添加行内联
  表单 → 脏队列（计数+离开提示）；保存 = 顺序 addCorrection（既有客户端
  函数），失败逐条带行上下文；成功后刷新预览与修正历史；i18n 双语
- [x] 2.2 测试：队列行为、保存顺序与失败隔离、未保存提示、模式切换、
  cell/api 两档同 UI（mock 端点同契约）

## 3. 收口

- [x] 3.1 api pytest 全量 + web vitest 全量 + tsc 全绿
- [x] 3.2 生产发布（create_all 建表随滚动自动发生，验证 dataset_corrections
  表在 PG 存在）与实弹验证（免费档修正+有效视图）
