## ADDED Requirements

### Requirement: 关系物化——数据集间语义连接

引擎提供 `customer_dataset_link` 工具：按（左集, 左列, 右集, 右列）把两个
cell 数据集连接为新派生数据集——LEFT JOIN 语义（左表行全保留，右列未命中
置 NULL，绝不取邻值）；右列与左列撞名时以 `right_` 前缀命名，未撞名右列保留
原名；子集主键=左集主键；行数超上限整批拒绝（子集保持原状）；血缘记录
kind="link"（parent=左集，spec 含右集与两侧列），`derive_refresh` 按记录
spec 重连。console 侧 `POST /api/v1/entities/{id}/relationships/{rel_id}/
materialize` 按关系声明一键执行：两端数据集均须 cell 档（否则 403 档位闸
如实）；子集登记为 derived（source_ref=左集），列与体量回填；关系或数据集
失存 404、列漂移 4xx 如实、引擎不可达 502、旧引擎 cell（无 link 工具）如实
报错；零半写（失败不落子集行）。

#### Scenario: 一键物化
- **WHEN** 客户对 cell 档两端的关系点「物化连接」
- **THEN** 生成连接数据集（左行全保留、命中行带右列值、未命中右列为空），
    出现在「我的数据」并挂上管线图（左父边）

#### Scenario: 撞名前缀
- **WHEN** 右集列名与左集列名冲突
- **THEN** 连接结果中该右列以 `right_` 前缀呈现，无冲突右列保留原名

#### Scenario: 档位闸
- **WHEN** 关系任一端数据集为免费档（api 档）
- **THEN** 403 档位闸如实（PLAN_TIER_REQUIRED 语义），零半写

#### Scenario: 重物化
- **WHEN** 对连接结果执行重派生（derive_refresh / 既有重派生入口）
- **THEN** 按血缘 spec 重连两侧最新数据，行数/列如实更新

#### Scenario: 旧引擎如实报错
- **WHEN** 目标 cell 引擎未含 link 工具（未滚动新 cell 镜像）
- **THEN** 物化端点返回可读错误（工具不存在），不伪造成功
