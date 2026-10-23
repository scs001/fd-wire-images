# E2E 手验剧本：wire-form-entry-advanced（任务 6.4）

> 状态：剧本就绪，**未执行**——需部署态 console（Logto 登录）+ 公网分享面。
> API 层语义已由 24 个新 pytest 用例覆盖（见下「已机器验证」）；本剧本补
> 的是浏览器 UX 层与真实双路径串联。

## 场景数据（一次备齐）

1. 数据集 `投入品档案`（api 档）：列 `名称(TEXT)`、`类别(TEXT)`、
   `安全间隔期天数(INTEGER)`；行：`药A/杀虫剂/7`、`药B/杀虫剂/10`、
   `草X/除草剂/1`。
2. 数据集 `打药记录`（api 档）：列 `记录日期(DATE)`、`地块(TEXT)`、
   `农药名称(TEXT)`、`截止日期(DATE)`。
3. 表单 A「打药录入」：字段=记录日期(date, required)、地块(text)、
   农药名称(select, optionsFromDataset=投入品档案.名称,
   filters=[{column:类别,op:eq,value:杀虫剂}])、
   安全间隔期天数(number, fillFrom={field:农药名称, column:安全间隔期天数})、
   截止日期(date, computed={op:add_days, date:记录日期, days:安全间隔期天数})。
4. 表单 B「采收录入」：字段=记录日期(date, required)、地块(text)、
   产量(number)；spec 级 constraint：
   `{id: safety_interval, message: 安全间隔期未到，禁止采收,
     probe: {datasetId: 打药记录, filters: [
       {column: 地块, op: eq, value: {field: 地块}},
       {column: 截止日期, op: gte, value: {field: 记录日期}}],
     expect: none}}`。

## 路径一：console（登录态）

1. 表单 A 详情 → 运行时：农药下拉仅 `药A/药B`（草X 被 filters 滤掉，升序）。
2. 选 `药A` → 天数字段自动填 `7`；记录日期填 `2026-10-15` → 截止日期自动
   填 `2026-10-22`（可手改）。**手改天数为 3 → 改选 药B → 天数被 10 覆盖**。
3. 提交 → 打药记录落一行（提交请求带 artifact_id；网络面板核对）。
4. 表单 B 运行时：地块填同值、记录日期填 `2026-10-20`（< 2026-10-22）→
   提交 → **422 呈现「安全间隔期未到，禁止采收」**，零写入。
5. 记录日期改 `2026-10-23` → 提交成功。

## 路径二：公开分享链接（免登录）

1. 表单 A 生成分享链接 → 无痕窗口打开：下拉正常（GET …/options 带 t，
   无 Authorization 头——网络面板核对）；限流前反复刷新正常（60s 缓存）。
2. 选 `药A` → 回填 `7` 来自 options 端点 fills（零额外请求）。
3. 提交成功 → 所有者修正历史可见 insert（actor=share-form:{id}）。
4. 表单 B 分享链接：同地块早于截止日提交 → **422 同文案**；晚于则成功。
5. 篡改探针：表单 A 提交农药名称=`药Z`（不在选项集）→ 422 拒。

## 顺带回归（存量零漂移）

- 任一旧表单（无新键）console 提交不带 artifact_id → 行为与从前一致。
- 旧分享表单提交/限流/404 形态不变。

## 已机器验证（pytest，勿重复手验）

- 公开选项端点：载荷/fills 字符串口径/截断/列漂移 error/统一 404/限流/
  60s 缓存/钉死版本（tests/test_share_form_options.py，8 用例）。
- console 执法：corrections artifact_id 归属矩阵/字段 422/闸命中/零漂移；
  cell 档引擎调用上界 ≤11（tests/test_form_entry_advanced.py，16 用例）。
- 双端一致性配对用例：web formSpec.test.ts 尾块 ↔ 后端首轮用例同夹具
  （药A/药B/7/10、2026-10-15+7）。

## 判定

两路径全过 + 零漂移回归过 = 6.4 可勾。任何一步与上述期望不符即挂起并
回填 tasks.md 备注。
