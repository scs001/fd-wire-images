## ADDED Requirements

### Requirement: 列表面板契约

看板面板类型谱 SHALL 新增 `list` 类型，用于把文本类快照渲染为图文信息流。
面板 MUST 携带内嵌 `snapshot`（与既有面板同形）；`encoding` MUST 声明
`title` 角色列，MAY 声明 `summary`、`image`、`time`、`source`、`link` 四个
可选角色列，各值均为快照列名。`config.limit` MAY 声明渲染行数上限
（1..20，缺省 20）。

校验 MUST 沿用既有宽容纪律：`title` 缺失、非字符串、或不在快照列内时，
该面板判坏并标注，其余面板照常渲染；可选角色的列名不在快照列内时 MUST
只丢该角色，面板其余部分照常渲染。无 `list` 面板的存量 spec 解析与渲染
MUST 零变化。

#### Scenario: 合法列表面板渲染
- **WHEN** 看板含一个声明 title/summary/image/time/source 五角色的 list 面板
- **THEN** 每个快照行渲染为一张卡片，五角色分别落在图片、标题、摘要、时间与来源位

#### Scenario: 标题列失效隔离
- **WHEN** list 面板的 title 列名不在快照列内
- **THEN** 该面板显示问题标注，同板其他面板正常渲染，看板不整板失败

#### Scenario: 可选角色降级
- **WHEN** list 面板声明了 image 角色但该列不在快照列内
- **THEN** 该面板按无图卡片渲染，不报错、不出现破图占位，其他角色正常

#### Scenario: 存量零漂移
- **WHEN** 打开不含 list 面板的存量看板
- **THEN** 渲染结果与本变更前逐字段一致

### Requirement: 卡片流渲染

list 面板 SHALL 按快照行序渲染卡片流，MUST NOT 在渲染层重排或去重
（顺序与新鲜度由刷新契约的 `query.sort` 决定，渲染与数据表视图同序）。
每张卡片 SHALL 呈现标题、摘要（如声明）、时间（如声明，按本地化短格式
展示原始值）、来源（如声明）与图片（如声明且值非空）。标题 SHALL 最多
两行、摘要最多三行，超出截断；声明 `link` 时卡片 SHALL 可点击跳转。

面板 SHALL 最多渲染 `config.limit` 行（缺省 20）；行数超出面板高度时
SHALL 面板内纵向滚动，不改变页面布局。空快照 SHALL 显示无数据占位。

#### Scenario: 行序即快照序
- **WHEN** 快照行按时间倒序排列
- **THEN** 卡片流按同一顺序呈现，渲染器不重排

#### Scenario: 长文截断
- **WHEN** 某行摘要长度超过三行
- **THEN** 卡片内摘要截断显示，卡片高度不失控，整板布局不变

#### Scenario: 行数上限
- **WHEN** 快照有 50 行且面板未声明 limit
- **THEN** 只渲染前 20 行，面板内可滚动查看

#### Scenario: 空快照诚实呈现
- **WHEN** list 面板快照为空
- **THEN** 显示无数据占位，不空白、不报错

### Requirement: 图文安全底线

list 面板的图片源与外链 MUST 只接受 `http(s)` 协议；`javascript:`、`data:`
与其他协议 MUST NOT 作为可加载源或可跳转目标。图片 MUST 声明懒加载与
`referrerPolicy="no-referrer"`；加载失败 MUST 隐藏图片块，不留破图占位。
外链 MUST 带 `target="_blank"` 与 `rel="noopener noreferrer"`。

标题、摘要、来源 MUST 按纯文本渲染，MUST NOT 解析 Markdown 或产生
`innerHTML` 注入点。此底线对控制台、分享页与橱窗页**同样适用**。

#### Scenario: 脚本协议不外跳
- **WHEN** 某行 link 值为 `javascript:alert(1)`
- **THEN** 卡片不产生可跳转目标，按无链接卡片渲染

#### Scenario: 图片协议过滤
- **WHEN** 某行 image 值为 `data:image/png;base64,...`
- **THEN** 该行不渲染图片块，其余内容照常显示

#### Scenario: 文本不被当作标记
- **WHEN** 某行标题含 `<script>` 或 HTML 标签
- **THEN** 按原文字面显示，不产生 DOM 元素、不执行脚本

### Requirement: 与既有面的兼容口径

list 面板 SHALL 与既有面板同权：整板 CSV 导出按该面板快照行原样导出一个
section（与 table 同口径）；市场与橱窗的面板摘要仍只取 `{id, type, title}`
三字段；分享页与橱窗页 MUST 复用同一渲染器渲染 list 面板，MUST NOT 新增
导出或其他写入入口。刷新侧 MUST 沿用既有口径：`query` 是刷新契约，list
面板的渲染数据源仍是快照。

#### Scenario: 整板导出含列表
- **WHEN** 客户对含 list 面板的看板导出 CSV
- **THEN** 该面板的 section 含快照列头与全部渲染行，与其他面板 section 并列

#### Scenario: 分享页零写入
- **WHEN** 打开含 list 面板的分享页
- **THEN** 卡片可看可点外链，但页面不存在编辑、保存、导出入口

#### Scenario: 刷新不改渲染源
- **WHEN** 带 query 的 list 面板完成一次契约刷新
- **THEN** 新版本按新快照渲染，未刷新时仍按旧快照渲染，渲染不实时查库
