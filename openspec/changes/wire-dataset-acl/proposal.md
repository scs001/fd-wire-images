# wire-dataset-acl

## Why

ROADMAP-v2 Wave B2：团队内**数据集级**授权缺失——现状是「成员见全队」：任何被邀
member（editor/viewer）可见、可读本队全部数据集，owner 无法把某几张敏感表
（薪酬/客户名单/合规底稿）收窄到指定成员。这是企业安全审查一眼能看出的洞，也是
B 波唯一未落项的治理缺口（B1 敏感度闭包已于 2026-10-30 上产归档，两者正交：B1 管
「数据多敏感」，本件管「谁能看」）。

## What Changes

- **数据集访问模式**：`datasets.acl_mode ∈ {team, restricted}`，默认 `team`——
  存量数据集零迁移、零行为变化；切 `restricted` 后仅 owner 与显式授权成员可见。
- **授权表**：`dataset_grants`（`dataset_id + logto_sub + permission ∈ {view, edit}`）
  与 append-only 审计表 `dataset_acl_events`（谁、何时、对哪张表改了什么），
  落数据集库（成员表在平台库，跨库以 `logto_sub` 弱引用，沿实体层跨库纪律）。
- **管理面（owner-only）**：`GET/PUT /api/v1/datasets/{id}/access`——读当前模式与
  授权名单、整表替换授权；非本企业成员 sub 400 如实拒绝；每次变更留痕。
- **拦截面（v1 清单）**：控制台数据集列表过滤 + 详情/列/行/质量/导出各面 403；
  数据面 console 轨道（query/aggregate/rows/export/六算子/sql 试运行）同闸；
  实体对象面（objects / objects/{key} / relations）对绑定受限数据集的请求 403
  （实体**结构**面 graph 不拦：元数据非数据）；受限数据集上的写操作需
  `permission=edit` 授权（全局角色门禁不变，两者叠加）。
- **状态码纪律**：队内无授权 = **403**（存在但无权，可读原因）；跨企业 = **404**
  （既有不泄露语义，逐字不变）。档位闸（cell 档/free-tier）顺序在 ACL 之前，
  既有 403 文案与测试零漂移。
- **不做（v1 边界，如实挂账）**：列级脱敏（ROADMAP 明写延后不排期）、数据面
  `wdk-` key 轨道（客户级凭据不承载成员身份）、MCP 工具面（wire-ops/入口均客户级
  凭据）、产物/看板快照面（已含数据的派生产物，v2 另裁）。

## Capabilities

### New Capabilities

（无——授权归团队空间鉴权域）

### Modified Capabilities

- `team-spaces`：新增「数据集级访问授权」——数据集 SHALL 可切 restricted 模式，
  仅 owner 与显式授权成员（view/edit）可读/可写；管理面 owner-only 且 append-only
  留痕；队内无授权 403、跨企业 404 语义 MUST 区分；`team` 模式存量行为逐字节不变。

## Impact

- **api**：迁移 0047（`datasets.acl_mode` + `dataset_grants` + `dataset_acl_events`）；
  `datasets/acl.py` 单一判定 helper（owner 旁路 / team 直通 / restricted 查 grant）；
  拦截点插入 datasets 各读面、`data_plane` console 轨道（principal 增 `member_sub`）、
  entities 对象三端点；管理端点 + 审计。
- **web**：数据集卡「受限」徽标；数据集详情「访问」面板（模式切换 + 成员授权表，
  owner 可见）；i18n zh/en；既有列表/详情回归零漂移。
- **兼容**：默认 `team`，存量数据集与全部既有测试逐字节等价；免费档口径不变。
- **时序**：与在途 `wire-entity-mn-materialize` 共享 `datasets/routes.py` /
  `data_plane/*` 文件——代码实施排在对方 commit 之后（本 change 先立提案与任务）。
