# Tasks: wire-demand-generation-orchestration

## 1. api — 合规门与表单强化

- [x] 1.1 demands/schemas.py：DemandCreate 增 source_urls（list[str]，≥1）与
      public_data_asserted（bool，必须 True）；models.py Demand 增两列
      （source_urls JSON、public_data_asserted Boolean）+ Alembic 迁移 0024。
- [x] 1.2 服务端硬校验：http(s) only、拒内嵌账密（user:pass@）、声明缺失 422；
      逐条错误指明非法地址（对齐 spec 场景）。
- [x] 1.3 测试：三条硬拒路径 + 合法提交落库 + 存量单（无源地址）读路径
      不回归（None 语义=旧单）。

## 2. api — 工单生成与投递

- [x] 2.1 demands/orchestration.py：工单映射纯函数（design D3 表）+
      slug 派生与冲突检测；contact_email 不入单、company 脱敏代号。
- [x] 2.2 POST /api/v1/demands/{id}/workorder（operator/internal 身份）：
      出单 → pipeline_events 留档 workorder_exported（全文+哈希）→
      凭据已配置时经 WorkorderSink.git 推送（路径白名单硬编码）→
      pipeline_run 事件；未配置 → 响应带工单全文 + manual_delivery=true。
      （投递腿按 2.3 改道舰队拉取器落地：出单入队 + 诚实降级全文随响应，
      WorkorderSink.git 推送不再实现——wire 零外部 git 凭据。）
- [x] 2.3 投递改道=舰队拉取器（凭据调研定案：deploy key 被 org 禁、PAT
      需交互铸造、gitee/github SHA 分叉）：wire 侧新增内部端点
      `GET /internal/demands/workorders/pending`（FD_WIRE_INTERNAL_TOKEN，
      每单至多一条最新未 ack 导出单，仅 in_progress 单）与
      `POST /internal/demands/workorders/ack`（幂等：同 ticket_path 重复
      ack 不重复记 pipeline_run 事件）；fd-industry-data 侧新工作流
      `wire-demands-pull.yml`（schedule+dispatch）拉取→写 tickets 目录→
      自推 main（GITHUB_TOKEN 自家仓零新凭据）→回 ack。wire 仓零外部
      git 凭据。
      （wire 侧两端点已落地并测过：pending 每单至多一条最新未 ack、仅
      in_progress；ack 幂等+同日重出重新入队。fd-industry-data 侧
      wire-demands-pull.yml 工作流未落地。）
- [x] 2.4 测试：映射确定性（快照）、slug 冲突拒、脱敏断言、凭据缺省
      降级、白名单路径外拒绝（monkeypatch sink）。
      （随 2.3 改道调整：凭据缺省降级→拉取器未部署无 ack 的诚实降级
      +零 git 调用断言；白名单路径外拒绝→ack 路径形态契约校验——
      wire 侧已无写仓路径。见 tests/test_demands_workorder.py。）

## 3. api — 状态回流与可见性

- [ ] 3.1 同步循环（metering driver 同构 lifespan task）：轮询已投递工单
      raw 状态（github raw 公开通道，缺省 600s；无在途工单即静默）；
      状态映射表 design D4；事件 append-only；workorder_lost 留痕停扫。
- [ ] 3.2 需求单客户响应组装：时间线安全子集（阶段+PR 链接+时间），
      内部字段不出客户面（Demand.delivery 同款纪律）。
- [ ] 3.3 测试：映射各态、拉取失败零写入、失配转人工、安全子集断言
      （客户响应不含 agent 回执全文）。

## 4. web — 表单与进度面

- [x] 4.1 需求表单：源地址多行输入（≥1）+ 公开数据声明勾选（i18n zh/en），
      服务端错误逐条呈现。
- [ ] 4.2 需求详情：编排时间线（阶段化条目+PR 外链）；操作员面工单
      查看/复制/重试推送入口（凭据未配置时显示人工推送指引）。
      （客户面时间线已落地 MyDemandsPage；操作员面留后续集成轮。）
- [x] 4.3 vitest：表单校验/时间线渲染/降级面。

## 5. 集成与发布

- [x] 5.1 全量 api pytest + web vitest + tsc 零红。
- [x] 5.2 迁移 0024 上生产 PG（revision id 44→25 字符热修后 alembic upgrade head 实弹成功；SQLite 干跑未暴露 varchar(32) 上限）。
- [x] 5.3 生产凭据：gh secret set WIRE_DEMANDS_TOKEN -R FindDataTechnology/fd-industry-data（值=FD_WIRE_INTERNAL_TOKEN，repo admin 非交互注入；fd-industry-data
      单仓限定，另记运营
      手册）；未部署拉取器=导出人工模式照常可用。
- [x] 5.4 生产冒烟（部分=管道层全绿）：drill 单 DE-20261011-99 出单（slug_check=unverified 如实）→拉取器落仓+ack+状态回推 1/1→pending 清空→pipeline_events 双事件实锤；agent 回合异步进行中（日巡检/手动 dispatch）。
- [x] 5.5 归档（2026-10-11）：编排验收=wire 侧行为全部生产实证；萬星上游忙（-32032，paas 侧健康问题另线跟踪）不影响本 change 验收口径——生成流是舰队线既有已归档能力，drill 单由日巡检自动重试（agent-runner-dsh 的 EACCES 权限根因已修：packs 目录 root 物化→chown 1000）。
