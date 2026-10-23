# wire-artifacts-persistence

## Why

产物（dashboard/组合指标/研究报告/告警/定时刷新五类）是 AI 分析的结晶，也是
应用平台（ROADMAP 方向 B 自定义看板）与个人工作台（方向 D 持续迭代体系）的
地基——但今天产物只以**会话草稿**存在：唯一端点是 `GET /api/v1/ai/conversations/
{id}/artifacts`，web 产物页的 spec 编辑保存仅落 localStorage（页面自标注"真保存
与物化是后续任务"），刷新即丢、无版本、无发布语义。没有产物转正，看板编辑器、
分享、个人迭代体系全部无从谈起。本 change 把产物升为一等实体：持久化、版本化、
状态机，为 B/D 方向奠基。

## What Changes

- **产物一等实体**：跨会话持久化的 artifact 记录（kind、title、spec JSON、
  状态、版本计数、来源会话、更新时间），客户域隔离。
- **版本化**：spec 每次保存追加新版本（append-only `artifact_versions`），
  可列出与查看任意历史版本；当前 spec 指针最新。
- **状态机**：`draft → published → archived`；published 是稳定引用位（后续物化/
  分享/定时刷新挂接点），archived 归档不删除；状态迁移留操作记录。物化执行本身
  不在本 change（refresh_schedule 的执行器接线是后续 change）。
- **API**：`GET /api/v1/artifacts`（列表，kind/status 过滤）、`GET /{id}`、
  `PUT /{id}`（spec 更新→新版本）、`GET /{id}/versions`、`GET /{id}/versions/{n}`、
  `POST /{id}/publish`、`POST /{id}/archive`；写操作限本人客户域。
- **会话草稿导入**：一键把会话草稿固化为 draft 产物（保留来源会话指针）；会话
  草稿读取端点不变。
- **web 产物页接真**：spec 编辑保存走服务端（localStorage 退位为未保存提示的
  本地缓冲），新增版本历史抽屉与发布/归档操作；五类 kind 徽标沿用。

## Capabilities

### New Capabilities

- `artifact-lifecycle`: 产物生命周期——持久化、版本化（append-only）、
  draft/published/archived 状态机、会话草稿导入、控制台编辑保存接真。

## Impact

- **fd-wire/api**：ai 模块新增 artifacts 持久层与路由（或独立 `artifacts` 子模块，
  实施时按 ai 模块边界定）、alembic 迁移（artifacts + artifact_versions）、
  客户域鉴权复用 console 会话。
- **fd-wire/web**：ArtifactsPage 接新 API（列表/编辑/版本/状态）、api.ts 函数、
  i18n 双语。
- **不碰**：AI 会话与草稿生成链路、产物 spec 的五类 schema 定义（本 change 只管
  生命周期不校验业务语义——spec 结构校验沿用现有最小校验口径）。
- **后续挂接**（不在本 change）：published 产物的物化执行、分享链接、定时刷新
  调度接线。
