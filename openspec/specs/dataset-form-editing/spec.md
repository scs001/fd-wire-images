# dataset-form-editing Specification

## Purpose
TBD - created by archiving change wire-dataset-form-editing. Update Purpose after archive.

## Requirements

### Requirement: api 档本地修正层

api 层数据集支持修正三操作（update 改值 / delete 删行 / insert 加行），
落本地覆盖层表（云端不破坏基行，永不丢原始数据）：控制台三端点
（POST corrections / GET corrections / revert）对 api 层数据集放行本地
实现，响应契约与 cell 档一致；列名不存在如实 400；悬空修正（锚行已不在）
保留并标注 dangling，不静默丢弃；回滚置 reverted_at（幂等拒绝二次回滚）；
写后同步数据集有效行数，insert 受免费档行数上限闸（超额拒绝，零部分执行）。

#### Scenario: 免费档改值即修正
- **WHEN** 免费档客户对 api 层数据集某行某列提交 update 修正
- **THEN** 修正落覆盖层返回 correction_id，行预览呈现新值，基行原值未破坏

#### Scenario: 删行加行与回滚
- **WHEN** 客户提交 delete 与 insert 修正后回滚其中一条
- **THEN** 回滚的修正不再生效（预览恢复），修正历史仍可见并标注已回滚

#### Scenario: 悬空如实
- **WHEN** update 修正的锚行已被删除
- **THEN** 修正保留在历史中标注 dangling，不报错不静默清除

#### Scenario: 行数闸
- **WHEN** insert 会使数据集有效行数超出免费档上限
- **THEN** 请求被拒并明示限额，不产生任何修正行

### Requirement: 有效视图统一

api 层数据集的全部读取路径呈现同一有效视图（基行 + 未回滚修正合并）：
控制台行预览与数据面 query/aggregate（wdk 轨道）一致；零修正数据集走
既有快路径，行为与性能零漂移。

#### Scenario: 两面一视图
- **WHEN** 客户对某 api 层数据集提交修正后分别经控制台预览与 wdk 查询读取
- **THEN** 两条路径看到相同的修正后数据

#### Scenario: 零修正零漂移
- **WHEN** 数据集没有任何未回滚修正
- **THEN** 读取路径与既有行为完全一致（快路径）

### Requirement: 双档统一行内编辑器

数据集详情行预览提供「编辑」模式（默认只读）：单元格双击改值、行删除、
「添加行」内联表单，编辑进脏队列（计数+未保存离开提示）；保存顺序提交
修正（两档同一端点契约），失败逐条带行上下文如实呈现；保存成功后预览与
修正历史刷新；cell 档与 api 档 UI 完全一致。

#### Scenario: 编辑即修正
- **WHEN** 客户在编辑模式改一格并保存
- **THEN** 提交一条 update 修正，预览刷新呈现新值，脏队列清空

#### Scenario: 批量保存失败隔离
- **WHEN** 队列中多条修改保存时某条被服务端拒绝
- **THEN** 已成功条目生效，失败条目带原因与行上下文呈现，未自动重试

#### Scenario: 未保存提示
- **WHEN** 脏队列非空时客户离开编辑模式
- **THEN** 提示未保存修改，确认后丢弃
