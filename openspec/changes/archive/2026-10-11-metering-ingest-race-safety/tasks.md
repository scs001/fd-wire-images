# Tasks

- [x] 1. ingest.py：两条写路径改方言分发 `on_conflict_do_nothing`（index_elements 对应唯一约束），rowcount 判定插入/跳过
- [x] 2. 测试：竞态吸收用例（同语句二次执行不炸、计重复）+ 双方言编译断言；既有 metering 套件回归
- [x] 3. 集成门：api 全量 pytest 通过 + tsc/web 不涉（零改动）
- [x] 4. 发布链：commit → push 三远端 → CI 绿 → relay 携带 → 滚版 → 生产 poll 通道健康验证
- [x] 5. 归档（delta 并入 metering-poll-channel，validate --strict 零 error）
