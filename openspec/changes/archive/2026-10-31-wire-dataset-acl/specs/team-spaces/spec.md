# team-spaces Delta — wire-dataset-acl

## ADDED Requirements

### Requirement: 数据集级访问授权

数据集 SHALL 支持两种访问模式：`team`（默认，本队全体成员可见——存量语义）与
`restricted`（仅 owner 与显式授权成员可见）。授权以成员（`logto_sub`）+ 权限
（`view|edit`）为粒度；owner（`billing_customers.logto_sub` 本人）MUST 恒有全权，
不受授权名单约束。模式切换与授权变更 MUST owner-only，且 MUST append-only 留痕
（谁、何时、旧新值）。判定 MUST 单一实现（同一 helper），禁止第二份授权语义。

拦截面（v1）：控制台数据集列表 MUST 过滤无 `view` 授权的 restricted 数据集；
数据集详情/列/行/质量/导出各面、数据面 console 轨道读面、实体对象面
（objects/objects/{key}/relations）MUST 403；restricted 数据集上的写操作 MUST 需
`edit` 授权（叠加既有全局角色门禁）。实体**结构**面（图谱、实体元数据）MUST NOT
受本授权约束（元数据非数据行）。

状态码语义 MUST 区分：队内无授权 403（存在但无权，detail 含可读原因）；跨企业
404（既有不泄露语义）。档位闸（free-tier / cell 档）判定顺序 MUST 在 ACL 之前，
既有 403 文案与行为零漂移。

#### Scenario: 受限数据集对未授权成员不可见

- **WHEN** 数据集切 `restricted` 且未授予成员 A（viewer）
- **THEN** A 的 `GET /api/v1/datasets` 列表中不含该行；`GET /api/v1/datasets/{id}`
  返回 403（detail 含「无访问授权」），不返回任何列/行数据

#### Scenario: 授权后可见、撤销后即不可见

- **WHEN** owner 授予 A `view` 后，A 再查列表与详情
- **THEN** 两处均正常可见可读；owner 随后撤销授权
- **THEN** A 的后续请求立即 403（无缓存窗口），审计表两条变更记录可查

#### Scenario: owner 恒可见

- **WHEN** 任意数据集处于 `restricted` 且授权名单为空
- **THEN** owner 的列表/详情/数据面读全正常；非 owner 成员全 403

#### Scenario: 数据面同闸

- **WHEN** 未授权成员以 console 轨道调用 `dataset_query` / `aggregate` / 导出
  或六算子读面，作用于 restricted 数据集
- **THEN** 403；对 `team` 模式数据集与既有档位 403 语义零变化

#### Scenario: 写面需 edit 授权

- **WHEN** editor 成员无 `edit` 授权，对 restricted 数据集发起批量写或派生
- **THEN** 403（授权不足）；被授予 `edit` 后同一调用正常，全局角色门禁仍独立生效

#### Scenario: 队内 403 与跨企业 404 区分

- **WHEN** 甲队成员请求乙队数据集（含 restricted 者）
- **THEN** 404（不泄露存在性，既有语义逐字不变）；同队 restricted 无授权则 403

#### Scenario: 零迁移兼容

- **WHEN** 存量数据集（`acl_mode` 默认 `team`）在无任何授权行时被任意成员访问
- **THEN** 列表、详情、数据面、对象面行为与变更前逐字节等价

#### Scenario: 管理面 owner-only 且留痕

- **WHEN** editor/viewer 调用 `GET/PUT /api/v1/datasets/{id}/access`
- **THEN** 403；owner 调用成功，`logto_sub` 不在本队成员/owner 名单时 400 如实拒绝，
  每次成功 PUT 落一条 append-only 审计行

#### Scenario: 实体对象面拦截、结构面不拦

- **WHEN** 未授权成员对绑定 restricted 数据集的实体调用 `objects`/`objects/{key}`/
  `relations`
- **THEN** 403；同一时刻 `GET /api/v1/entities/graph` 与实体元数据列表正常返回
  （结构可见、数据不可见）
