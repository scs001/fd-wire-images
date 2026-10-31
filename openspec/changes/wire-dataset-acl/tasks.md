# wire-dataset-acl — Tasks

## 1. api：存储与迁移

- [x] 1.1 迁移 0047（双方言、create_all 兜底幂等）：`datasets.acl_mode`
  varchar(16) NOT NULL DEFAULT 'team'；`dataset_grants`（UNIQUE dataset_id+logto_sub）；
  `dataset_acl_events`（append-only）——已实施，down_revision=0046
- [x] 1.2 模型 + 真链实证：sqlite 真链 upgrade / create_all→stamp→upgrade / 双跑
  幂等均实测；PG 方言按 JSON().with_variant(JSONB) 落（PG 真链随 6.3 rollout 冒烟）

## 2. api：判定与拦截

- [x] 2.1 `datasets/acl.py` 单一判定 helper（owner 旁路 / team 直通 / restricted
  查 grant，read|write 两档）；`DataPlanePrincipal` 增 `member_sub`（console 轨道
  取 sub，data-key 轨道 None）——另携带 console_customer/role（跨库取 customer
  的传输字段，非第二判定）
- [x] 2.2 控制台面：列表过滤；详情/列/行/质量/导出 403；顺序=404→档位闸→ACL
- [x] 2.3 数据面 console 轨道：query/aggregate/lineage/sql_preview/batchWrite/
  columns/derive/join/filter/project/aggregate-derive/union/sql/refresh/schedule 全闸
- [x] 2.4 实体对象面：objects/objects/{key}/relations（含 m:n）403；graph 与元数据读不拦
- [x] 2.5 写面：restricted 上 PUT/批量写/派生/物化需 edit 授权（叠加角色门禁）

## 3. api：管理端点与审计

- [x] 3.1 `GET/PUT /api/v1/datasets/{id}/access`：owner-only；整表替换幂等；
  非本队 sub 400 如实（含计数）；`mode=team` 清空 grants
- [x] 3.2 每次成功 PUT 落 `dataset_acl_events`（旧/新快照 payload），同事务

## 4. api 测试（对准坏的东西本身）

- [x] 4.1 授权矩阵：owner / 有 view / 无授权 / 有 edit × 列表·详情·数据面·对象面·
  写面，逐格断言 403/200/404 与「零数据泄露」——tests/test_dataset_acl.py 20 测
- [x] 4.2 撤销即时生效、模式切回 team 即恢复全员可见、审计行逐条对账
- [x] 4.3 兼容回归：存量 team 数据集全量既有测试零漂移；跨企业 404 逐字不变；
  档位闸 403 文案不变（`test_tier_gate_precedes_acl_and_text_unchanged`）
- [x] 4.4 api 全量套件回归绿——ACL 落地后 1640 passed / 11 skipped；并入 window
  腿后 1666 passed / 11 skipped

## 5. web

- [x] 5.1 数据集卡「受限」徽标；详情「访问」面板（模式切换 + 成员授权表 + 撤销，
  owner 可见，editor/viewer 隐藏入口）——`DatasetAccessSection.tsx`（owner 经
  `GET /api/console/team` 的 me.role 判定；api 侧 `DatasetOut.acl_mode` 已补，
  徽标按响应值渲染）；`DatasetAcl.test.tsx` 8 测
- [x] 5.2 i18n zh/en 同批；web 全量套件回归绿——66 文件 / **1056 passed**；tsc 净

## 6. 文档与交付

- [x] 6.1 文档「数据集」章节「访问授权」双语小节（含 v1 边界四条：wdk 轨道/MCP/
  产物快照/列级脱敏）——团队空间章节新增 `docs-team-spaces-acl` 卡（zh/en）
- [ ] 6.2 `openspec validate wire-dataset-acl --strict` 绿
- [ ] 6.3 rollout（双库真链）+ 真库冒烟：restricted 切档→成员列表过滤→授权→
  数据面 403→撤销即 403，全链如实
- [ ] 6.4 closeout：wire-ops-mcp 工具面零改动→无壹座重烘焙前置；ROADMAP B2 勾选
