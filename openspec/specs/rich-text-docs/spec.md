# rich-text-docs Specification

## Purpose
定义正文型产物（kind=doc）：客户在柏讯控制台书写长文档（报告/文案/说明书）
的富文本能力——Markdown 为正文事实源、分栏实时预览、插图以外部链接承载、
渲染层面结构性地不可注入脚本。使柏讯具备「人写的文档」这一产物形态，与既有
结构化产物（看板/表单/记录页）并列。

## Requirements

### Requirement: 文档正文契约

doc 产物的 spec 信封MUST为 `{title, spec:{format:"markdown", text}}`：`format`
恒为 `"markdown"`（v1 唯一格式），`text` 为 Markdown 源文本（SHALL NOT 存储
预渲染 HTML）。正文体积 SHALL 有上限（缺省 256 KB，可配置）；超出上限、format
缺失/非 markdown、text 非字符串一律 422 如实拒绝且零半写（不追加版本行）。

#### Scenario: 空白创建
- **WHEN** 客户以 kind=doc 空白创建文档并起标题
- **THEN** 生成 draft 产物（v1=`{format:"markdown", text:""}` 的最小 spec），
    可直接进分栏编辑器写作

#### Scenario: 格式校验
- **WHEN** 保存的 doc spec 缺 format、format 非 markdown、或 text 非字符串
- **THEN** 422 如实拒绝且零半写（不追加版本行），错误定位到字段

#### Scenario: 正文上限
- **WHEN** 保存的正文超过上限
- **THEN** 422 如实拒绝并载明上限（字节数），不落版本

#### Scenario: 存量零漂移
- **WHEN** 保存非 doc 产物的 spec
- **THEN** 校验与保存行为与本变更前逐字一致

### Requirement: 渲染安全底线

文档正文的渲染MUST在结构上不可注入脚本：渲染SHALL NOT 产生 `innerHTML`
注入点（渲染为宿主框架的原生节点）；Markdown 中的原始 HTML SHALL NOT 被
执行/渲染为 DOM；链接 SHALL 带 `noopener noreferrer`；图片源 SHALL 仅接受
`http(s)` 协议（`javascript:`/`data:` 等一律不作为可加载源）。此底线对编辑
预览与公开分享页**同样适用**。

#### Scenario: 脚本不执行
- **WHEN** 正文包含 `<script>`、事件处理属性（如 `onerror=`）或
    `javascript:` 链接、`data:text/html` 图片源
- **THEN** 预览与分享页均不执行/不加载，仅按纯文本或安全降级呈现

#### Scenario: 原始 HTML 不渲染为 DOM
- **WHEN** 正文包含任意 HTML 标签
- **THEN** 渲染结果中不出现该标签对应的 DOM 元素

#### Scenario: 外链图片
- **WHEN** 正文包含 `![alt](https://host/img.png)`
- **THEN** 预览与分享页以图片呈现（带 alt）；`![](javascript:...)` 不呈现为
    图片

### Requirement: 分栏编辑器

控制台MUST提供 doc 的编辑面：Markdown 源编辑区与实时预览区（分栏）；
自动保存MUST落**草稿层**（见 artifact-lifecycle「草稿层与版本链分离」），
界面如实呈现保存状态（未保存/保存中/已保存/失败）；「保存版本」MUST把当前
草稿追加为版本并可见既定版本历史。归档（archived）产物 SHALL 只读。

#### Scenario: 实时预览
- **WHEN** 客户在编辑区输入 Markdown
- **THEN** 预览区同步呈现渲染结果（标题/强调/列表/引用/代码/链接/图片）

#### Scenario: 自动保存与显式版本
- **WHEN** 客户停止输入片刻
- **THEN** 草稿层被自动保存且状态显示「已保存」；未点「保存版本」时版本链
    不新增

#### Scenario: 归档只读
- **WHEN** 文档产物处于 archived
- **THEN** 编辑器拒绝写入（保存报错如实呈现），恢复为 draft 后可继续编辑

### Requirement: 文档分享

published 的 doc 产物MUST可生成签名分享链接（复用既有令牌机器：HMAC 签名、
钉死生成时刻版本、TTL 档、归档即失效）；公开页 SHALL 渲染钉死版本的正文，
且适用「渲染安全底线」。分享页 SHALL noindex（搜索引擎不可收录）。

#### Scenario: 分享只读呈现
- **WHEN** 持有效令牌访问 doc 分享链接
- **THEN** 呈现该钉死版本的正文渲染页，无编辑入口

#### Scenario: 归档撤销
- **WHEN** 文档产物被归档
- **THEN** 既有分享链接失效（404）

#### Scenario: 改版不串版
- **WHEN** 文档在分享后又保存了新版本
- **THEN** 既有分享链接仍呈现生成时刻钉死的版本

### Requirement: 文档正文 SHALL 支持 agent 经 MCP 写入

doc 正文 SHALL 可由 AI agent 经产物构建 MCP 写入，正文契约（`{format:"markdown", text}`、256 KB 上限、零半写）SHALL 与控制台书写路径完全一致。agent 写入的文档 SHALL 记录生产来源指针；SHALL NOT 引入第二套正文格式或预渲染 HTML 存储。

#### Scenario: agent 写入正文

- **WHEN** 舰队岗位经 MCP 创建或更新一个 doc 产物
- **THEN** 正文按 markdown 契约落库，版本链与控制台书写路径一致

#### Scenario: 超限与格式错误同规

- **WHEN** agent 提交的正文超过体积上限或 format 非 markdown
- **THEN** 422 如实拒绝且零半写，与控制台路径行为一致

#### Scenario: 来源可区分

- **WHEN** 读取一个由 agent 写入的 doc
- **THEN** 可读出其生产来源指针，与人工书写的文档可区分
