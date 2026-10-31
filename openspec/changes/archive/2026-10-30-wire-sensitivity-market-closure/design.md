# wire-sensitivity-market-closure — Design

单一设计点：**消费根仓 helper，不第二实现**。根仓 `wire-sensitivity-propagation`
交付 `effective_sensitivity(customer_id, dataset_ids)`（闭包 + inherited/incomplete
标注，零引擎调用）；本 change 把 `market/artifact_service.py` 的 `_sensitivity_of`
替换为该 helper 的调用，发布闸（产物可见性档位）与渲染复检（Q5 双点闸）两个
调用点自动获得闭包口径。

- incomplete（父集不全）在产物面同语义 fail-closed：按「命中敏感」处理并如实
  文案（与数据集面 D3 的区分——数据集面目录显示自身声明+警示，产物面闸直接拒，
  因为产物面没有「显示警示」的替代安全位）。
- helper 未上产前本 change 不可上产（依赖前置）；接口形状以根仓 change
  design D4 为契约。
- 渲染复检在每次渲染请求实时执行（既有 Q5 纪律），无缓存——祖先改回常规
  即恢复（spec 场景已定）。
