# free-tier-data Specification

## Purpose
定义免费档（无 cell）的轻量数据集能力：api 侧存储、上传通道、个数与行数限额、
REST 查询聚合可用面，以及付费面（连接器/派生/批量写/MCP 物化）的档位闸——使
「我的数据」对免费用户真实可用而非升级墙，同时付费档既有行为零变化。

## Requirements

### Requirement: 免费档 api 层数据集

免费档客户可创建 api 侧存储的数据集（`storage_tier=api`）：上传 CSV 建集、查看
列与类型、经数据面 REST 查询与聚合；全程不创建 cell 资源、不调用 cell 引擎。

#### Scenario: 免费上传即用
- **WHEN** 免费档客户上传 CSV 建集并查询
- **THEN** 建集成功、schema 与行数如实呈现、查询/聚合返回正确结果，且全程不发生任何 cell 引擎调用

#### Scenario: 与 cell 层数据集同列共存
- **WHEN** 客户列表自己的数据集
- **THEN** api 层与 cell 层数据集在同一列表呈现，存储层以徽标区分

### Requirement: 免费档限额

免费档数据集限额为配置项（默认：≤3 个数据集、每集 ≤10,000 行、仅上传通道）；
超限操作被拒且错误信息含余量与上限；限额触达时控制台内联呈现升档引导而非
整页拦截。**档位未知时（quota 端点 `max_*` 为 null 的 fail-closed 口径），
控制台配额条 MUST 降级为「档位信息不可用」中性提示行，MUST NOT 抛错或
整块崩溃；限额数值存在时呈现语义与文案逐字不变。**

#### Scenario: 建集超限
- **WHEN** 免费档客户试图创建第 4 个数据集
- **THEN** 请求被拒，错误含「已用 3/3」，控制台内联提示升档

#### Scenario: 行数超限
- **WHEN** 上传件行数超过每集上限
- **THEN** 写入被拒并返回上限与实际行数，不产生半写状态

#### Scenario: 档位未知降级
- **WHEN** quota 响应 max_datasets 或 max_rows_per_dataset 为 null（档位
    解析 fail-closed 口径），客户打开「我的数据」页或工作台
- **THEN** 配额条渲染中性提示（如「当前档位限额信息不可用」），页面其余
    功能不受影响；限额恢复有值后呈现自动回到既有语义

#### Scenario: 正常路径零漂移
- **WHEN** quota 响应限额数值齐全
- **THEN** 配额条渲染与防护引入前逐字一致（含触达上限的 amber 态与升档引导）

### Requirement: 付费面档位闸

连接器建集、derive/join 派生、批量写、MCP/AI 物化需要 cell 层（≥基础档）；
免费档调用这些路径获得明确的档位错误码与升级说明，入口在控制台可见但标注
付费。

#### Scenario: 免费档请求派生被拒
- **WHEN** 免费档客户对 api 层数据集发起 derive 或 join
- **THEN** 请求被拒并明示需付费档，不产生任何部分执行

### Requirement: 计量与限流口径

api 层数据集的读取不产生行数计量入账（与客户数据读取不计额的既有口径一致）；
限流沿用数据面既有每 key 速率限制。

#### Scenario: 免费读取不计额
- **WHEN** 免费档客户多次查询自己的 api 层数据集
- **THEN** metering_records 不新增行数入账，速率限制照常生效

### Requirement: 付费档零回归

基础档及以上客户的数据集默认 cell 层，上传/连接器/派生/批量写行为与本 change
前一致；升档客户的 api 层数据集保持可用。

#### Scenario: 付费行为不变
- **WHEN** 付费档客户执行既有上传→物化→派生链路
- **THEN** 行为与结果与本 change 前一致（回归测试基线）

### Requirement: 档位解析确定性与同名守卫

客户档位解析（`plan_code_for`/`storage_limit_for` 的 billing_customers 按
name 查询）MUST 为**确定性单行解析**：同名多行时按「`logto_sub` 非空行
优先 → `id` 最小者」收敛到一行，MUST NOT 因多行抛异常或返回不确定行；
全无 sub 的同名行才按 None（fail-closed，方向不变）。客户建档两路径
（`ensure_customer` 自开户、admin 建档）MUST 带同名守卫：同 `name` 已有
任意档位行时——`ensure_customer` 幂等返回既有行（跨档同名即复用，不再
造第二行）、admin 建档 422 如实拒并提示改名；`billing_customers.name`
MUST 有数据库唯一约束（迁移先按「sub 非空优先，无 sub 孤儿行加
`#orphan-<id>` 后缀」自愈改名，再建唯一索引；与 2026-10-15 生产手工止血
规则一致）。

#### Scenario: 孤儿行不再劫持档位
- **WHEN** 同名存在一行 sub 非空（free）与一行 sub 为空（standard）的
    客户行，免费档链路调用 plan 解析
- **THEN** 解析收敛到 sub 非空行（free），免费建档/quota 正常，不抛
    MultipleResultsFound 也不返回 None

#### Scenario: 自开户撞同名幂等
- **WHEN** 新 sub 首登触发自开户，而库中已有同名（email 同名）客户行
- **THEN** 该 sub 的映射落库到既有行（`logto_sub` 写入），不再新建第二行

#### Scenario: admin 建档重名拒
- **WHEN** admin 试图创建与既有行同名的客户
- **THEN** 422 如实拒（文案定位 name 已存在），零半写

#### Scenario: 迁移自愈
- **WHEN** 库中存在同名多行（含无 sub 孤儿行）时执行迁移
- **THEN** 孤儿行先被改名（加 `#orphan-<id>` 后缀）消除重名，唯一索引
    建立成功；真户行（sub 非空）名称不变

### Requirement: 构建器多步配置状态保持

单记录页与表单构建器 MUST 支持多步连续配置不丢失已保存到草稿态的选择：
主数据集→键列→头部字段→段/字段配置的任意后续交互（含增删段、勾选其他
字段、切换编辑/查看）MUST 保持先前已配置项的值；「移除段/删除字段」等
显式删除操作不受此限。草稿态保存（spec PUT）后的重载呈现 MUST 与保存
内容一致。

#### Scenario: 多步配置一次走通
- **WHEN** 客户在单记录页构建器依次：选主数据集→选键列→勾头部字段→
    添加段并选段数据集/日期列→添加过滤条件
- **THEN** 此前每一步的选择全部保持，保存后 spec 与界面配置一致

#### Scenario: 显式删除不受限
- **WHEN** 客户点击「移除段」或删除字段
- **THEN** 该项被移除，其余配置保持
