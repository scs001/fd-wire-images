# demand-orchestration Specification

## Purpose
需求单→生成爬虫的编排闭环：把 in_progress 需求单确定性映射为 health-loop
生成工单（脱敏红线），经舰队拉取器投递到 fd-industry-data 公开仓（wire 零
外部 git 凭据），状态经推送式回流进客户可见时间线；出单入口覆盖内部端点
与操作员控制台双闸动作，投递诚实降级、绝不呈现自动化成功假象。

## Requirements

### Requirement: 生成工单 SHALL 确定性映射

操作员对 in_progress 需求单触发生成工单时，系统MUST按固定映射产出
health-loop 生成工单 YAML（kind=generate、unit 按源域名派生且仓内已存在
即拒、brief 携带 source_urls/expectations/cadence/notes、golden 声明即
强制、verify 挂标准验证命令），并把工单全文与哈希 append-only 留档于
pipeline_events（workorder_exported）——同一需求单可重出新工单，历史不
改写。工单内容MUST NOT包含客户联系邮箱；公司以脱敏代号留档（公开仓
红线）。

#### Scenario: 出单
- **WHEN** 操作员对带合法源地址的 in_progress 需求单点击「转生成工单」
- **THEN** 产出符合 health-loop 契约的 YAML 工单并留档，需求单状态不变

#### Scenario: slug 冲突如实拒
- **WHEN** 派生的 unit slug 在目标仓已存在
- **THEN** 出单被拒并提示转人工（非 greenfield 不走生成流）

#### Scenario: 脱敏红线
- **WHEN** 工单生成完成
- **THEN** 工单文本不含客户邮箱；公司名以脱敏代号呈现

### Requirement: 工单投递 SHALL 凭据门控且诚实降级

工单投递MUST经舰队拉取器通道：wire 暴露内部拉取端点（内部令牌鉴权）供
fd-industry-data 工作流拉取待投递工单、落仓自推、并回 ack；wire 侧MUST
NOT持有任何外部仓 git 凭据。拉取器未部署时控制台MUST展示工单全文供
人工推送并明示「等待舰队拉取器/人工推送」——MUST NOT在未真实投递时展示
任何自动化成功假象。ack 幂等（同工单重复 ack 不重复记事件）。

#### Scenario: 拉取投递
- **WHEN** 舰队拉取器凭内部令牌拉取待投递工单并回 ack
- **THEN** 工单文件由对方仓工作流落 tickets 路径，wire 侧 pipeline_run 事件留档含可追溯路径

#### Scenario: 诚实降级
- **WHEN** 拉取器未部署（无 ack）
- **THEN** 控制台展示工单全文与人工推送指引，无自动投递假成功文案

#### Scenario: ack 幂等
- **WHEN** 同一工单被重复 ack
- **THEN** 不重复追加事件，返回既有已 ack 状态

#### Scenario: 内部端点鉴权
- **WHEN** 无内部令牌访问拉取或 ack 端点
- **THEN** 401，与既有内部端点口径一致

### Requirement: 状态回流 SHALL 只写真实进度

工单状态MUST经舰队推送回流：拉取器从 tracked 清单取在途工单路径、读
其本地 checkout 的工单文件全文、回推 wire（内部端点），wire 解析映射为
pipeline_events 追加事件与客户可见时间线条目（已投生成/待人工审核/
审核通过点亮/受阻跟进），PR 链接留档；回推幂等（重复推送零追加）。
wire 侧MUST NOT在无真实来源时写入任何进度条目；YAML 不可解析MUST 400
如实回绝。轮询式回流（wire 主动拉公开 raw）仅作出站可达环境下的兜底，
MUST可用环境变量关闭（fd-prod 海外出站被挡，生产默认关）。

#### Scenario: PR 开启回流
- **WHEN** 拉取器回推的工单文件含 pr_url 非空的生命周期条目
- **THEN** pipeline_events 记 pr_open 事件（含 PR 链接），客户时间线出现待审核条目

#### Scenario: 回推幂等
- **WHEN** 同一工单文件被重复回推且状态未变
- **THEN** 零追加事件（幂等去重）

#### Scenario: 坏载荷如实回绝
- **WHEN** 回推内容不是可解析的工单 YAML
- **THEN** 400 回绝并带原因，不写入任何进度

#### Scenario: 生产零出站
- **WHEN** 部署环境无法访问公开 raw 通道（如 fd-prod 海外出站被挡）
- **THEN** 状态回流不受影响（推送式），轮询兜底经环境变量关闭

### Requirement: 客户进度 SHALL 安全子集可见

需求详情时间线MUST展示编排阶段的安全子集（阶段名+PR 链接+时间），
agent 回执全文、重试细节、内部错误串MUST NOT出现在客户面；终态交付
MUST仍由操作员经既有交付端点完成——编排MUST NOT自动置 delivered。

#### Scenario: 安全子集
- **WHEN** 客户查看处于生成流水线中的需求单详情
- **THEN** 时间线呈现阶段化进度与 PR 链接，无任何内部细节字段

#### Scenario: 编排不自动交付
- **WHEN** 工单达到合并点亮终态
- **THEN** 需求单仍为 in_progress，交付由操作员既有端点执行

### Requirement: 操作员控制台 SHALL 提供出单动作

admin 派发台（双闸鉴权面）MUST为需求单提供「转生成工单」动作，复用与
内部端点完全相同的出单 service 语义（确定性映射、脱敏红线、三态 slug
探测、append-only 留档、出单不迁移状态）；仅 `in_progress` 单可出（其余
409 如实回绝）。成功响应 MUST在控制台展示工单全文（折叠）与
ticket_path、slug_check 释义，并明示「已入队未投递——等待舰队拉取器
（约 30 分钟）或人工推送」；MUST NOT展示任何自动化投递成功假象。失败
（无源地址 422、slug 占用 409）MUST在控制台如实回显服务端文案。

#### Scenario: 控制台出单
- **WHEN** 操作员在 admin 派发台对带合法源地址的 in_progress 需求单点击「转生成工单」
- **THEN** 走与内部端点同一 service 完成出单留档入队，页面展示工单全文与未投递诚实文案，需求单状态不变

#### Scenario: 控制台失败如实回显
- **WHEN** 操作员对无 source_urls 的单、slug 已占用的单或非 in_progress 的单触发出单
- **THEN** 控制台按服务端口径（422/409/409）如实回显原因，不产生任何留档

#### Scenario: 双闸继承
- **WHEN** 未过 admin 双闸（Bearer ID token 缺失/无 wire-admin 角色，或 X-Admin-Token 不符）访问控制台出单端点
- **THEN** 401/403 回绝，与 admin 端点族既有口径一致

### Requirement: 生成工单舰队 schema 合规

wire 侧生成的 health-loop 工单 SHALL 满足舰队工单 schema（`fd_industry_data.health.ticket.validate_ticket`）的必填字段：`ticket_id`（与落盘文件名主干一致）、`source`、`unit`、`created_at`（出单时刻 UTC ISO）、`evidence`（dict）、`verify`、`lifecycle`（初始为空列表）。`evidence` SHALL 仅含脱敏事实（需求单编码、客户代号、源地址、公开数据声明），SHALL NOT 含 contact_email 或公司名。同日多次出单的 `ticket_id` SHALL 保持一致；`created_at` 按每次出单时刻如实记录。

#### Scenario: 出单通过舰队验票
- **WHEN** 对任一带合法源地址的需求单执行转生成工单
- **THEN** 产出的工单 YAML 含全部必填字段且类型合法（等价于舰队 validate_ticket 零错误），萬星生成回合的 ticket_valid 验证环不再因工单自身缺字段判红

#### Scenario: 脱敏事实进 evidence
- **WHEN** 工单构造完成
- **THEN** evidence 含需求单编码与客户代号，不含 contact_email 与公司名

#### Scenario: ticket_id 与文件名一致
- **WHEN** 工单以文件名 `<YYYYMMDD>-wire-<demand.code>.yaml` 落盘
- **THEN** YAML 内 ticket_id 等于该文件名去 `.yaml`，同日重复出单不漂移

### Requirement: 管理台交付 SHALL 同语义自动开通私有 scope

admin 派发台的 delivered 迁移 MUST与内部端点逐字同语义：迁移成功后自动发起
客户私有 scope 交付（复用既有 `maybe_auto_deliver` 钩子），scope 面失败只落
`scope_status=pending` 与留档、绝不打断迁移响应。MUST NOT存在「管理台交付
留 scope_status=None、客户私有数据面永不开通」的通道不对称。

#### Scenario: 管理台交付自动开 scope
- **WHEN** 操作员在 admin 派发台把 in_progress 单迁到 delivered
- **THEN** 迁移成功响应照常返回，且 scope 交付已自动发起（成功=scope_status
  ok，失败=pending 留档）——与内部端点行为一致

#### Scenario: scope 失败不打断
- **WHEN** 自动交付钩子执行中 scope 面不可用
- **THEN** 迁移仍成功（delivered），scope_status=pending，留档可经重试端点补课

### Requirement: 运营进度面 SHALL 呈现编排全事件

admin 派发台 MUST为每张需求单呈现编排进度面（数据源=`GET
/internal/admin/demands` 已返回的 pipeline_events/delivery_events/gate/
scope_status/scope_name，MUST NOT新增聚合端点）：事件时间线按序展示
workorder_exported（已出单，含 ticket_path）、pipeline_run（已投递）、
workorder_submitted（已入生成流水线）、pr_open（待人工审核，含 PR 链接）、
merged（已合并点亮）、blocked（受阻）、workorder_lost（工单丢失）各阶段的
发生时间；gate 摘要（通过/尝试/流水线运行数）；交付状态（scope 就绪/
挂起）。未出单的单 MUST如实占位「尚未出单」，MUST NOT虚构进度。

#### Scenario: 在途单进度可见
- **WHEN** 操作员展开一张已出单且工单在生成流水线中的需求单
- **THEN** 可见已出单→已投递→已入生成各阶段时间与 ticket_path，后续阶段
  如实缺席（未发生即不渲染）

#### Scenario: 未出单占位
- **WHEN** 操作员展开一张从未出单的需求单
- **THEN** 进度面显示「尚未出单」占位，无任何虚构阶段

### Requirement: 客户交付就绪 SHALL 可见且无内部细节

控制台「我的需求」页 MUST在 delivered 单上渲染交付就绪信号：私有数据面
已就绪（scope_status=ok）/准备中（pending）；已授权转公开时呈现公开状态。
非 delivered 单 MUST NOT渲染该区块；delivery_events 全文、失败错误串、
内部字段名 MUST NOT出现在客户面（安全子集纪律同编排时间线）。

#### Scenario: 交付就绪呈现
- **WHEN** 客户查看一张 delivered 且 scope_status=ok 的需求单
- **THEN** 列表该单出现「私有数据面已就绪」语义的交付区块

#### Scenario: 非交付单不渲染
- **WHEN** 客户查看 submitted/in_progress/rejected 单
- **THEN** 无交付就绪区块；页面无任何 delivery_events 内部细节字段

### Requirement: 出单闸 SHALL 复检禁爬源

工单导出 MUST对需求单存量 source_urls 复检禁爬清单：命中 MUST 409 如实
拒绝并说明命中平台，MUST NOT 产出含禁爬源的工单、MUST NOT 留下
workorder_exported 留档（零半写）。复检与提交口共用同一清单与匹配规则
（单一出处）。

#### Scenario: 存量单带禁爬源出单被拒
- **WHEN** 运营对一张（历史遗留或直接入库的）source_urls 含 pinduoduo.com 的 in_progress 单触发出单
- **THEN** 409，文案点名平台；pipeline_events 零新增

#### Scenario: 合规单出单不受影响
- **WHEN** 运营对 source_urls 全部合规的单触发出单
- **THEN** 行为与既有逐字一致（工单产出+留档+入队）

### Requirement: 受理后 SHALL 自动出单（后台补扫）

系统 MUST 以后台 tick 循环（lifespan 挂载，缺省间隔 60s、环境变量可调）
定期补扫「in_progress 且无任何出单尝试留档（既无 `workorder_exported`
也无 `auto_export_failed`）」的需求单，对每单自动调用既有出单 service
（`export_workorder_service`，语义/留档/拒绝口径逐字复用）送入舰队拉取
队列——使受理（submitted→in_progress）后无需任何人工点击即完成出单，
同时兜住受理时点前后的进程重启/瞬时异常。自动出单成功时
`workorder_exported` 事件 MUST 带 `trigger: "auto"` 标注（人工出单不带
此键，既有历史与人工路径零漂移）；被拒时（无源地址/slug 占用/禁爬复检）
MUST NOT 产出工单、MUST NOT 回滚任何状态，MUST 落一条
`auto_export_failed` 事件（含 reason 与 status_code）供运营面如实呈现。
补扫 MUST 幂等：已有任一尝试留档的单（含 `auto_export_failed`——拒绝原因
均为需人工处置类，MUST NOT 重试防事件刷屏）绝不重复动作；单次 tick 中
某单的失败 MUST NOT 中断其余单的补扫与循环本身。循环 MUST 可经环境变量
关闭（缺省开），关闭时受理照常、零出单事件、人工出单入口不受影响。

#### Scenario: 受理后自动出单
- **WHEN** 操作员把带合法源地址的 submitted 单迁移到 in_progress，随后
  补扫 tick 运行
- **THEN** 该单出现 workorder_exported 留档（trigger=auto，含 ticket_path
  与 sha256），且出现在舰队拉取队列（pending 端点可见）；全流程无任何
  人工出单动作

#### Scenario: 出单被拒如实留痕
- **WHEN** 补扫遇到一张无 source_urls 的存量在途单（或 slug 已占用的单）
- **THEN** 落一条 auto_export_failed（reason 含服务端拒绝文案），无
  workorder_exported，需求单状态与其余事件零变化

#### Scenario: 补扫幂等
- **WHEN** 已出单或已有 auto_export_failed 的 in_progress 单经历任意多轮
  tick
- **THEN** 不产生任何新事件（逐字节不变），其余漏网单照常补出

#### Scenario: 关闭开关
- **WHEN** 环境变量把自动出单关闭
- **THEN** 补扫不动作（受理照常成功、零出单事件），人工「转生成工单」
  入口行为不变

### Requirement: 运营进度面 SHALL 呈现自动出单失败与自动标注

admin 派发台进度面 MUST 渲染 `auto_export_failed` 阶段（含失败原因），
操作员可见「自动出单被拒·需人工处置」并据此使用既有「转生成工单」人工
入口重试；`workorder_exported` 为自动触发（trigger=auto）时 MUST 呈现
自动标注。未出单占位（「尚未出单」）MUST 只在无任何出单尝试留档时显示
——有 auto_export_failed 即 MUST NOT 再显示该占位。

#### Scenario: 失败可见
- **WHEN** 操作员展开一张自动出单被拒的需求单进度面
- **THEN** 时间线含「自动出单被拒」阶段与失败原因，且不再显示
  「尚未出单」占位

#### Scenario: 自动成功标注
- **WHEN** 操作员展开一张自动出单成功的单
- **THEN** 「已出单」阶段带自动触发标注，其余阶段渲染不变

#### Scenario: 占位语义
- **WHEN** 操作员展开一张从未有任何出单尝试的 in_progress 单
- **THEN** 进度面显示「尚未出单」占位（原语义不变）

### Requirement: 交付 scope 重试端点

平台 SHALL 暴露交付 scope 重试端点（内网令牌与管理台两路同语义）：
`POST .../demands/{id}/scope-retry`——仅对 status=delivered 且
scope_status=pending 的需求单受理（其余 409 如实回显当前状态与 scope 态）；
执行语义复用交付动作的既有重试分支（不迁移状态，重试建私有 scope+逐指标
grant），成功置 scope_status=ok 并留档 delivery_events，失败照旧留档并保持
pending；请求可携带可选 indicators/source_dbs 覆盖（缺省 linked_indicators）。

#### Scenario: pending 补课成功

- **WHEN** 对 delivered+pending 且上游 scope API 可用的单发起重试
- **THEN** scope 建立与授权成功，scope_status=ok、scope_name 落档，delivery_events 追加 scope_created

#### Scenario: 非法状态拒

- **WHEN** 对 in_progress 或 delivered+scope_status=ok 的单发起重试
- **THEN** 409，detail 如实回显当前状态与 scope 状态，零副作用

#### Scenario: 重试再失败如实

- **WHEN** 重试时上游仍不可达或拒（如空指标单空规则被 400）
- **THEN** 响应含失败原因，scope_status 保持 pending，delivery_events 追加 scope_create_failed

### Requirement: 运营面 scope 重试与配置接线

管理台需求单页 SHALL 对 delivered 且 scope_status=pending 的单呈现「重试开
scope」动作（点击调管理端点，结果如实呈现：成功显示 scope 名，失败显示新错
误文案）；生产部署 SHALL 配置 SCOPE_API_BASE（fd-prod 内网 scope REST 面地址）
与 SCOPE_API_TOKEN（secret）——未配置时交付与重试照既有诚实降级（pending
留档），配置后新交付自动开 scope。

#### Scenario: 运营补课

- **WHEN** 运营对 pending 单点击「重试开 scope」且上游可用
- **THEN** 按钮态流转为成功，进度/交付面呈现 scope 名与 ok 态

#### Scenario: 未配置仍诚实

- **WHEN** SCOPE_API_BASE 未配置时交付或重试
- **THEN** scope_status=pending，delivery_events 留档「scope API 未配置」，绝不虚构成功

### Requirement: 萬星直连调度 SHALL 同映射同红线且留痕可审

操作员对 in_progress 需求单触发萬星直连调度时，系统 MUST 按与生成工单
完全相同的确定性映射与脱敏红线构造工单 YAML（含禁爬源复检与 slug 三态
探测：occupied 拒、free/unverified 放行），经 A2A `message/send` 直连
spider-heal（调用键经 secret 注入、新域 wanxing.finddatatech.cloud、
`Idempotency-Key` 头），成功后 MUST 把投递事实 append-only 留档
（`wanxing_dispatched` 事件：工单路径/全文/哈希、幂等键、message id、
HTTP 状态、agent 回执摘录）。直连单 MUST NOT 进入舰队拉取队列
（拉取队列只认 `workorder_exported` 事件——通道互斥由操作员选择、事件
类型天然区分）。同一工单内容重复直连 MUST 派生同一幂等键（wanxing 侧
去重口径）。

#### Scenario: 直连成功
- **WHEN** 操作员对带合法源地址的 in_progress 需求单点击「萬星直连」且 A2A 调用 2xx
- **THEN** 响应携带工单全文与 a2a 回执（http_status/message_id/idempotency_key/reply_excerpt），需求单时间线追加 `wanxing_dispatched`，状态不迁移，拉取队列不收录该单

#### Scenario: 幂等键稳定
- **WHEN** 同一需求单以同一工单内容重复直连
- **THEN** 两次派生的 Idempotency-Key 相同（`wire-<code>-<sha256 前 16 位>`）

#### Scenario: 出单闸复用
- **WHEN** 非 in_progress、或缺 source_urls、或命中禁爬源、或 slug 已占用
- **THEN** 与「转生成工单」同一拒绝口径（409/422），零事件留档

### Requirement: 直连失败 SHALL 如实降级绝不假成功

萬星直连凭据缺失（env 未配置）MUST 503 如实回绝（不静默回落舰队通道）；
A2A 调用失败（网络不可达、401/403 调用键或白名单问题、非 JSON 响应）
MUST 记 `wanxing_dispatch_failed` 事件（error/reason 留档）并以 502 回绝，
响应与失败事件 MUST 携带工单全文——人工推送降级通道始终可用，MUST NOT
在任何失败形态下呈现自动化投递成功假象。

#### Scenario: 未配置如实拒
- **WHEN** `WANXING_CALLER_KEY` 未配置时操作员触发直连
- **THEN** 503 明示「萬星直连未配置」，零事件留档

#### Scenario: 调用失败留档降级
- **WHEN** A2A 调用返回 403（调用者不在白名单）或网络不可达
- **THEN** 502 携带错误定位与工单全文，时间线追加 `wanxing_dispatch_failed`（含 error/reason），不追加 `wanxing_dispatched`

### Requirement: 直连端点 SHALL 内部令牌与控制台双闸

萬星直连调度 MUST 同时暴露内部端点（`X-Internal-Token` 与
`FD_WIRE_INTERNAL_TOKEN` 比对，401 口径与既有内部端点一致）与操作员
控制台动作（admin 鉴权），两闸共用同一 service——语义、响应形状、拒绝
口径完全一致。

#### Scenario: 内部端点鉴权
- **WHEN** 无内部令牌调用 `POST /api/v1/demands/{id}/wanxing-dispatch`
- **THEN** 401，与既有内部端点口径一致

#### Scenario: 双闸同口径
- **WHEN** 同一需求单分别经内部端点与控制台动作触发直连
- **THEN** 走同一 service（`dispatch_wanxing_service`），响应形状与拒绝口径逐字段一致
