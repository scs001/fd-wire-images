## 1. 契约层：list 类型与宽容归一

- [x] 1.1 在 `web/src/console/dashboard/spec.ts` 的面板类型谱新增 `list`（放在基础档，与 kpi/line/bar/table 同组），扩展 `DashboardPanelEncoding` 或新增 list 专用角色类型（title 必填，summary/image/time/source/link 可选），验证：`npx tsc --noEmit` 通过，既有 `spec.test.ts` 全绿
- [x] 1.2 新增 list 编码归一：角色值非字符串或空串丢弃；title 缺失/不在快照列内 → 面板进坏面板标注（reason 新增 `bad_list_encoding`）；可选角色列名不命中只丢该角色，验证：单测覆盖合法、title 失效、可选角色失效、全空四种输入
- [x] 1.3 `config.limit` 归一为 1..20 的整数（缺省 20，越界钳制），验证：单测断言 0/21/非数三种输入的归一结果
- [x] 1.4 存量面板解析零漂移：对既有 v1/v2 fixture 做深比较断言输出与升级前一致，验证：`vitest run spec` 全绿

## 2. 渲染层：卡片流与安全底线

- [x] 2.1 `DashboardRenderer.tsx` 新增 list 分支与 `ListPanel` 子组件（图片块、标题、摘要、时间、来源、外链），按快照行序渲染，验证：组件测试断言三行快照渲染三张卡片、顺序与快照一致
- [x] 2.2 实现截断与滚动：标题两行、摘要三行（CSS line-clamp），行数超 `config.limit` 只渲染前 N 行，超出库高面板内纵向滚动，验证：组件测试断言渲染行数、容器 overflow 样式
- [x] 2.3 图片安全与降级：仅 `http(s)` 源、`loading="lazy"`、`referrerPolicy="no-referrer"`、onError 隐藏图片块、空值不渲染图片块，验证：组件测试覆盖合法 URL、data: 源、空值、模拟 error 四种情形
- [x] 2.4 外链安全：仅 `http(s)` 可跳转，`target="_blank"` + `rel="noopener noreferrer"`；非法协议按无链接卡片渲染，验证：组件测试断言 href/rel 属性与非法协议不产生锚点
- [x] 2.5 文本纯渲染：标题/摘要/来源走文本节点，含 HTML 标签时按字面显示，验证：组件测试断言容器内无新增 DOM 元素
- [x] 2.6 空快照占位：无行时显示无数据占位，验证：组件测试断言占位文案存在且无卡片

## 3. 编辑器与导出

- [x] 3.1 新增面板类型选择出现 `list`（基础档分组内），选中后生成骨架：两列（title/summary）空快照 + 默认 title 角色，验证：组件测试断言选项存在、选中后面板落库且 shape 合法
- [x] 3.2 面板详情区新增角色映射表单（五个可选角色各一个列选择器 + limit 输入），已有 encoding 回填，验证：组件测试改角色并保存后 spec 含新 encoding、快照未被改写
- [x] 3.3 类型互转语义：list 与 table 互转保 snapshot；list 与图表类互转走既有确认（信息取舍），验证：单测断言互转后快照零损失、encoding 按规则保留或清理
- [x] 3.4 整板 CSV 导出把 list 面板按快照行导出一个 section（与 table 同口径），验证：单测断言导出内容含该 section 的列头与行

## 4. 生成侧与回归

- [x] 4.1 `api/fd_wire_api/ai/tools.py` 的 `dashboard_spec_create` 指令补 list 写法与适用条件（文本列 + 信息流意图才用；必须声明 title；不得用 table 假扮信息流），验证：后端测试断指令文案含 list 关键约束
- [x] 4.2 分享页与橱窗页回归：含 list 面板的 spec 在两页正常渲染，且无编辑/导出入口，验证：组件测试断言两页卡片存在、写入入口为零
- [x] 4.3 市场/橱窗面板摘要回归：list 面板摘要仍只含 `{id, type, title}`，验证：后端测试断言摘要字段集合不变
- [x] 4.4 刷新回归：带 query 的 list 面板走既有契约刷新后仅 snapshot 变化、v2 字段与 encoding 完整保留，验证：后端测试断言新版本 encoding 逐字段不变
