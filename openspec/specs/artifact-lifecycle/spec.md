# artifact-lifecycle Specification

## Purpose
定义分析产物（dashboard/composite_indicator/research_report/alert_rule/
refresh_schedule 五类）的生命周期管理：跨会话持久化、append-only 版本化、
draft/published/archived 状态机、会话草稿导入，以及控制台产物页的服务端接真——
使 AI 生成的分析成果可保存、可迭代、可稳定引用，为看板编辑、分享与物化执行
奠基。

## Requirements

### Requirement: 产物持久化

产物MUST为客户域内的一等实体：跨会话持久保存（kind、标题、spec、状态、
来源会话指针、更新时间）；客户只能访问自己的产物；会话草稿可一键固化为
draft 产物且保留来源指针，会话草稿读取端点行为不变；**空白新建**——
console 会话可直接创建空白 dashboard、form、record-view 或 doc draft 产物
（v1=最小空 spec；form 见 form-apps 能力、record-view 见 record-view 能力、
doc 见 rich-text-docs 能力），kind 白名单 v0 为 dashboard、form、record-view
与 doc，title 必填。

#### Scenario: 草稿转正
- **WHEN** 客户把某会话草稿导入为产物
- **THEN** 生成 draft 产物并可跨会话访问，来源会话指针如实记录

#### Scenario: 客户域隔离
- **WHEN** 客户 A 请求客户 B 的产物
- **THEN** 返回 404，不泄露存在性

#### Scenario: 空白建板
- **WHEN** 免费档客户（无 cell）新建看板并起标题
- **THEN** 生成 draft 产物（空面板 spec），可直接进编辑器加面板/绑定数据

#### Scenario: 空白建表单
- **WHEN** 免费档客户（无 cell）新建表单并起标题
- **THEN** 生成 draft 产物（零字段、未绑数据集的最小 form spec），可直接进
    构建器绑数据集配字段

#### Scenario: 空白建溯源页
- **WHEN** 免费档客户（无 cell）新建单记录公开页并起标题
- **THEN** 生成 draft 产物（未绑数据集的最小 record-view spec），可直接进
    构建器绑主数据集配头部与时间轴

#### Scenario: 空白建文档
- **WHEN** 客户新建文档并起标题
- **THEN** 生成 draft 产物（空正文的最小 doc spec），可直接进分栏编辑器写作

#### Scenario: kind 白名单
- **WHEN** 请求以空白方式创建 dashboard、form、record-view 与 doc 之外的 kind
- **THEN** 422 如实拒绝（其他 kind 仍由 AI 草稿转正产生）

### Requirement: 版本化

产物 spec 的每次保存追加一个不可变版本（append-only）；当前版本为最高版本号；
任意历史版本可列出与查看；archived 产物拒绝 spec 修改（恢复后可改）。

#### Scenario: 保存即新版本
- **WHEN** 客户编辑产物 spec 并保存两次
- **THEN** 版本号递增且两个历史版本均完整可查，历史版本不可被修改

### Requirement: 状态机

产物状态为 draft → published → archived 单向迁移，archived → draft 恢复允许；
每次迁移留操作记录；published 是对外稳定引用位——**published 仪表盘可生成签名
分享链接（见 dashboard-share），archived 后链接即失效**；物化执行挂接点在本
能力之外。

#### Scenario: 发布与归档
- **WHEN** 客户依次对 draft 产物执行发布、归档
- **THEN** 状态依次迁移且操作留档；归档后 spec 修改被拒并提示恢复路径

#### Scenario: 非法迁移拒绝
- **WHEN** 对 archived 产物直接执行发布
- **THEN** 返回 409 并说明合法迁移路径

#### Scenario: 归档断分享
- **WHEN** 产物归档后既有分享链接被打开
- **THEN** 链接失效（404），恢复 published 后需重新生成链接

### Requirement: 控制台产物页接真

产物页的列表、spec 编辑保存、版本历史、发布/归档操作全部走服务端 API；
localStorage 仅作未保存离开提示的本地缓冲，不再是保存目的地。

#### Scenario: 编辑不丢失
- **WHEN** 客户编辑产物 spec 保存后刷新页面
- **THEN** 重新打开看到已保存的最新版本内容与版本号

### Requirement: 草稿层与版本链分离

带正文的产物（v1 = doc，见 rich-text-docs 能力）MUST支持**草稿层**（自动保存）
与**版本链**（显式保存）双层模型：自动保存MUST覆盖写草稿层且不产生版本行；
显式保存MUST把草稿层内容追加为不可变版本；草稿层 SHALL NOT 参与状态机判定、
marketplace 可见性判定与分享钉版；版本链 append-only 语义与既有产物逐字相同。
草稿层 SHALL 按产物一对一存续（产物删除时一并清除）。

#### Scenario: 草稿层与版本链分离
- **WHEN** 客户在编辑器里连续自动保存多次后才点一次「保存版本」
- **THEN** 版本链只新增一个版本；草稿层始终持有最新正文；刷新页面后未保存
  的正文不丢失

#### Scenario: 草稿层不泄漏
- **WHEN** 产物处于 draft 状态且从未显式保存过版本
- **THEN** 其他会话/入口读到的正文是草稿层最新内容，且不存在任何版本行

### Requirement: 带内容创建 SHALL 限平台租户且与空白创建同校验

`POST /api/v1/artifacts` 的入参 SHALL 支持可选 `spec` 字段：携带 spec 时一步创建
「带内容 draft 产物」（v1 版本行 = 给定 spec），走与 spec 更新相同的 kind 分派校验
（`validate_kind_spec`）；校验失败 422 如实拒绝且零半写（不落任何产物/版本行）。
带 spec 的创建 SHALL 仅限平台自营发布方（`finddata-official`）：其他租户携带 spec
创建一律 403。不带 spec 的请求 SHALL 保持既有空白创建行为完全不变（全租户可用）。

#### Scenario: 平台租户带内容建档

- **WHEN** 平台租户 POST `{kind:"dashboard", title:"…", spec:{title:"…", panels:[…]}}`
  且 spec 通过 kind 分派校验
- **THEN** 生成 draft 产物，v1 版本内容为给定 spec
- **AND** 版本行 operator 与初始状态留痕语义与空白创建一致

#### Scenario: 非平台租户带内容被拒

- **WHEN** 普通租户 POST 携带非空 `spec`
- **THEN** 403 如实拒绝，不产生任何产物或版本行

#### Scenario: spec 校验失败零半写

- **WHEN** 携带的 spec 未通过 `validate_kind_spec`（如 dashboard 缺 panels、字段类型错）
- **THEN** 422 带原因拒绝，库中无新增产物行与版本行

#### Scenario: 不带 spec 行为不变

- **WHEN** 任一租户 POST 仅带 `{kind,title}`（与既有空白创建入参一致）
- **THEN** 行为与现状逐字节一致（最小空 spec、全租户可用、kind 白名单 422 语义不变）

### Requirement: showcase_slug 幂等 SHALL 同租户查重追加版本

携带 spec 的创建 SHALL 支持 `spec.showcase_slug` 作为幂等键（沿用 import_showcase
既有约定）：同租户内已存在同 slug 产物时，重复 POST SHALL NOT 创建第二条产物，
而是向既有产物追加一个新版本（append-only，语义与 spec 更新一致）并返回既有
产物标识；跨租户的同 slug 互不可见、互不命中。

#### Scenario: 同 slug 重放追加版本

- **WHEN** 平台租户以同 slug 第二次 POST（spec 内容有更新）
- **THEN** 不新建产物，既有产物版本号递增且新版本内容为本次给定 spec
- **AND** 响应如实标识既有产物（非新建语义）

#### Scenario: 同 slug 重放内容相同

- **WHEN** 平台租户以同 slug 第二次 POST 且 spec 与当前版本内容一致
- **THEN** 不新建产物、不追加冗余版本，返回既有产物当前状态

#### Scenario: 跨租户 slug 隔离

- **WHEN** 普通租户（若有带内容权限的后续演进）或平台租户使用与其他租户
  相同的 slug 值
- **THEN** 幂等查重只在请求方租户域内进行，不因他租户同 slug 而被拒或被合并

### Requirement: 产物 SHALL 记录生产来源指针

产物 SHALL 支持可选的生产来源指针，标识该产物由哪个部署、哪个舰队岗位、哪个回合产出；指针随版本行留痕，append-only。手工创建与控制台 AI 创建的产物 SHALL 保持来源为空或标为人工，其余生命周期语义（draft/published/archived、版本链、草稿层）SHALL NOT 因该字段改变。

#### Scenario: 舰队产物带来源

- **WHEN** 产物经产物构建 MCP 由舰队岗位创建
- **THEN** 其版本行携带来源指针（部署、岗位、回合），且可在产物详情读出

#### Scenario: 手工产物来源为空

- **WHEN** 客户在控制台手工创建或经控制台 AI 草稿转正
- **THEN** 来源指针为空或标为人工，版本链与状态机行为与现状逐字节一致

#### Scenario: 来源不参与鉴权

- **WHEN** 任意来源的产物被读取、更新或分享
- **THEN** 客户域隔离与权限判定只看产物归属，不看来源指针
