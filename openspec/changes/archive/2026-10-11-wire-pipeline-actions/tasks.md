# Tasks: wire-pipeline-actions

- [x] 1.1 api client：refreshDataset(id, cascade)（POST /datasets/{id}/refresh?
  cascade=，响应含 cascade 列表）、getDatasetSchedule/putDatasetSchedule/
  deleteDatasetSchedule（PUT/GET/DELETE /datasets/{id}/schedule）
- [x] 1.2 DatasetDetail：derived+cell 档显示「重派生」（级联开关+结果列表
  呈现+详情刷新）与「定时重派生」（cron 登记/态呈现/删除+UTC 注记）；
  api 层数据集无入口；i18n 双语
- [x] 1.3 测试：重派生触发（Bearer+cascade 参数）、级联结果呈现、失败级
  如实、定时登记/呈现/删除、免费档无入口断言
- [x] 1.4 web vitest 全量 + tsc 全绿
