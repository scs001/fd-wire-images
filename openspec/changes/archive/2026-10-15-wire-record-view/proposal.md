# Proposal: wire-record-view（单记录公开页：record-view 产物 + 长效分享 + 二维码）

## Why

农业溯源台账需求清单（finddata 根目录 `农业溯源台账系统开发需求清单.xlsx`）
的 M4/M5：为每个批次生成二维码，消费者扫码打开**按批次参数化的时间轴
溯源页**（播种→施肥→打药→采收→加工→出库，含合规标识与联系方式）。这不是
看板（dashboard 公开页是版本钉死的快照渲染、零 fetch），也不是表单——它
是**按键取行的公开实时只读页**：标签印刷后数据还在补录/修正，扫码必须
看到最新有效视图。

两个结构缺口：

1. **公开面零数据读路径**：share 面目前只有 form submit（写-only）与
   dashboard/form 的 spec 快照。单记录页需要第一个「验签后按声明查询取行」
   的公开读面——暴露边界必须成为一等契约而非实现细节。
2. **令牌 7/30/90 天上限对印刷物不成立**：二维码贴在包装上流通数月到数年，
   链接过期=产品变「无溯源」。需要**长效档（不设期）**，撤销语义归位到
   归档/取消发布（既有机器）。

通用价值：record-view = 「数据集 + 键 + 展示模板」的参数化公开页，不止
溯源——检测报告页、证书页、产品履历页同构复用。与 wire-form-entry-advanced
（录入侧）合起来，构成台账场景「录入→管理→公开」三段的前两批通用件。

## What Changes

- **新产物 kind `record-view`**（artifact-lifecycle 白名单 v0 +record-view，
  空白创建）：spec 信封 `{title, spec:{datasetId, keyColumn, headerFields[],
  sections[]}}`——主记录数据集+键列（如批次号）、头部字段（产品名/产地/
  负责人/电话，label 可改写）、sections（时间轴源：数据集+日期列+过滤条件
  值可引用主记录字段+行上限）。
- **公开实时取数端点** `GET /api/v1/share/record-view/{id}?t=&k=`（令牌验签
  +钉死版本）：按 k 等值查主记录一行（零行=如实「无此记录」页），sections
  过滤代入主记录字段值取行（按日期列升序、每段 ≤上限缺省 200），返回
  结构化结果由 web 渲染移动优先时间轴页；**读边界=仅执行钉死版本 spec 声明
  的查询**，不开放任意查询；令牌级限流（缺省 240/小时）+ 短缓存；noindex。
- **长效令牌档**（dashboard-share TTL 白名单 +`0`=不设期，三类 kind 同享）：
  生成时 UI 明示印刷物场景；撤销=归档/取消发布即全链失效（既有语义）。
- **二维码生成**：record-view 详情面「输入/选键值 → 生成分享链接 + 下载
  二维码（PNG 高清/SVG）」，纯前端生成零后端依赖。
- **构建器**：主数据集+键列选择、头部字段勾选与 label 改写、sections 配置
  （含引用主记录字段的过滤条件）、按示例键值试渲染预览。
- **展示面收录**：workbench 徽标/产物页收录 record-view（web 与 api 展示
  序两处一致）；文档新章节（含首个公开实时读面的暴露边界如实说明）；
  i18n zh/en。

## Impact

- 代码：`api/fd_wire_api/artifacts/{models,service}.py`（kind 白名单/空白
  spec/校验分派）、`share/{service,routes,rate_limit}.py`（TTL 0 档、
  SHARE_KIND_PATHS、新取数端点）、`artifacts/query_contract.py`（sections
  查询复用 `_query_dataset`）、`workbench.py`、`web/`（构建器 RecordViewEditor、
  公开页 ShareView 新 mode + 时间轴渲染、二维码库引入、api.ts 展示序）。
- 兼容：既有 dashboard/form 分享行为零漂移（TTL 白名单追加 0，缺省仍 30
  天）；产物枚举新增 kind 对存量消费面（workbench/AI 门）为纯追加。
- 红线新增：**首次公开实时数据读面**——边界收在「钉死版本声明的查询 +
  令牌门 + 限流 + noindex + 行数上限」；同一令牌可查看该产物任意键值记录
  （k 不进令牌，枚举边界如实文档，不谎报按记录隔离）。
- 风险：公开读面的查询成本（缓存+限流兜底）；key 列低熵枚举（印刷批次号
  本就公开，如实披露）；二维码库供应链（选小而稳的纯前端库）。

## Non-Goals

- **按记录隔离令牌**（每批次独立令牌）：v1 令牌门在产物级，k 开放——溯源
  页本来就是公众可见；需要强隔离的场景另行立项。
- **附件/证书图片展示**（绿色食品证书扫描件）：依赖文件资产能力（与
  form 文件字段同一缺口），后批合并立项。
- **AI 生成 record-view**：v1 手动构建器优先；AI 门（ARTIFACT_TOOL_KINDS）
  不收录。
- **批量二维码打包下载**（zip）：v1 逐键生成下载。
- **二维码短链/自定义域名**：直出完整分享 URL。
