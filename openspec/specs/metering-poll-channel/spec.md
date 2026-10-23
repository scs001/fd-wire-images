# metering-poll-channel Specification

## Purpose
定义 wire 计量入账对 registry 审计流的归集通道口径：直读（拉模式）为正道、可达性以活探针为闸门、探针不过按推送（推模式）降级——保证真实客户调用在有限时间内入账，不静默丢账。

## Requirements

### Requirement: 直读通道入账

直读通道配置达成后，一次真实客户调用须在有限时间（一个轮询周期余量，目标 10 分钟内）内于 wire 计量表出现对应行，且来源标注正确。

#### Scenario: 真实调用按时入账
- **WHEN** 客户经 wire 入口完成一次计费调用，且直读通道已配置
- **THEN** 10 分钟内 wire 侧计量表出现该调用的行，凭据轨道与来源标注正确

### Requirement: 可达性闸门

直读通道的落地以一次真实出站可达性探针为闸门（从 wire 运行环境探测 registry 数据面的只读入口）；探针不通过时不得宣称通道已建成。

#### Scenario: 探针不过不开闸
- **WHEN** wire 运行环境到 registry 数据面只读入口的出站探针失败
- **THEN** 直读通道标记为未达成，走降级路径而非带病上线

### Requirement: 降级到推送且不丢账

直读不可达时按推送口径降级：registry 侧审计事件推送至 wire 既有接收端；降级期间与直读期间一样，任何已发生的计费调用不得静默丢失。

#### Scenario: 降级期间入账连续
- **WHEN** 直读探针失败、系统按推送口径运行，期间发生计费调用
- **THEN** 该调用仍进入 wire 计量表（时间上允许晚于直读口径，但必须到达）

### Requirement: 摄取竞态安全

计量摄取服务（HTTP 推送与轮询归集共用）在并发摄取同一事件时 SHALL 以数据库唯一约束原子吸收冲突：冲突键已由并发事务提交的场合，本事务 SHALL 将该事件计为重复（`duplicates`/`anomaly_duplicates`）并继续处理批内其余事件，MUST NOT 因唯一约束违规使整批摄取失败或回滚。

#### Scenario: 并发同键摄取不炸批

- **WHEN** 两个摄取事务（如手动排空与自动 tick）先后处理同一 `request_id + server_name`（异常另含 `reason`）的事件，且后者的 SELECT 预检发生在前者提交之前
- **THEN** 后者的 INSERT 被唯一约束以 ON CONFLICT DO NOTHING 吸收，该事件计为重复，批内其余事件照常入账，事务不回滚

#### Scenario: 正常重复重放语义不变

- **WHEN** 同一事件在无并发的情况下被重放摄取（预检可见历史行）
- **THEN** 计数仍为重复且不产生第二行，行为与既有幂等口径一致

#### Scenario: 双方言一致

- **WHEN** 摄取服务运行在 PostgreSQL（生产）或 SQLite（测试）任一方言
- **THEN** 冲突吸收语义一致，均不抛出唯一约束违规
