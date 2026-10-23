# 实施报告 — add-artifact-create-endpoint（2026-10-22）

实施 commit：`81ffaba`（源仓 FindDataTechnology/fd-wire main）
生产镜像：`sha-6f8c8ed`（快照线 scs001/fd-wire-images@6f8c8ed，快照=源真 81ffaba；
私有仓 GHA 额度必败复发，走既有正道）；roll commit `ef0102c`。
relay 手动补单一次（cron 全轮尚未轮到首见 tag）。

## 测试

- api 全量 1410 passed / 11 skipped（既有 PG 方言跳过）；新增：
  - lifecycle +7：平台带内容 201、非平台 403 零半写、校验失败 422 零半写、
    slug 重放追加版本 200、内容一致不冗余、archived 重放 409、跨租户 slug 隔离
  - showcase-import +1：**真 uvicorn 全链**——run_import_http 连跑两次，
    首跑 created、次跑 updated 零新建版本+1、橱窗不翻倍
- 空白创建既有行为逐字节不变（原 17 条 lifecycle 测试零改动全绿）

## 生产验收（4.2）

- `import_showcase --http-base-url https://wire.finddatatech.cloud`（本机，脱离 pod）
  重放 5 张省级板：**5/5 succeeded，id 31–35 不变，版本稳定 v3（两轮连跑零冗余）**；
  公开橱窗仍 14 张（省级 5 张在列）。重放中如实检出并同步了 5 张板的 note 演进
  （v2→v3 各一版，与本地最新构建一致）。

## 实施中抓出的两处真缺陷（已修）

1. **平台租户在控制台面无人可达**：`finddata-official` 的 `logto_sub=None`、
   `customer_members` 空——resolved 路径三条全断，控制台根本进不去平台租户
   （这正是此前只能 pod 脚本上板的深层原因）。处置：aloadtree（Logto sub
   `pih8r05mwh8w`）入 `customer_members`（owner）+ `member_active_team` 指针
   指向 13——self-serve 自有客户行的 owner 回退会压过成员行，指针是唯一
   干净通道。此后平台板可全程控制台管理。
2. 首次全量重放 5/5「failed」假象：POST/追加版本其实都成功（5 张板各 +1 版），
   失败在最后一步 `PUT /visibility`——同一租户错配（aloadtree 解析到自有
   行，404）。指针落好后全链绿。**教训：failed 条目必须透出 error 字段给
   汇总视角**（本次报告层已带 error，是查看脚本截掉了）。

## 通道记录

- Logto token 铸造（Experience API + PKCE，aloadtree 身份）已验证可用，
  脚本 `/tmp/logto_mint_token.py`（会话临时件；token 用后即焚）。
- scs001/fd-wire-images 为新建公开快照仓（secrets 已配 TCR），
  后续 fd-wire 出片默认走此线。
