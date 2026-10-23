# Tasks

## 1. api（form_spec.py 纯函数层）

- [x] 1.1 结构校验：`visibleWhenGroup{mode∈all/any, conditions 1..10}`（每条复用单条件校验器；与 visibleWhen 互斥）；`optionsFrom{field, cases, fallback?}`（select 与 options 互斥恰有其一；cases 键非空串、值非空字符串列表；fallback 同列表规）；引用存在性/自引用二趟覆盖组条件与 optionsFrom.field
- [x] 1.2 求值：`evaluate_hidden_columns` 扩展——组条件（all 全真/any 一真）+ optionsFrom 解析后空选项列并入隐藏集（解析口径：raw values 控制值 trim 字符串命中 cases→列表，未命中/空→fallback→[]）
- [x] 1.3 提交校验：select 分支动态成员校验（值 ∈ 解析后选项；空选项已按隐藏处理）；隐藏字段携带非空值拒（覆盖空选项列）
- [x] 1.4 测试：校验矩阵（互斥×2/空组/超 10 条/cases 键值形状/自引用）、求值新例（all 真/假、any、空选项隐藏）、动态成员防篡改 422、对拍用例表扩充；变异自证（去掉互斥校验→红；去掉空选项并入隐藏→红）

## 2. web

- [x] 2.1 `formSpec.ts`：类型+镜像校验（normalize/validate 同 api 逐字同规）+求值镜像（组条件+动态选项解析+空选项隐藏）
- [x] 2.2 `FormEditor.tsx`：组条件编辑器（条件行增删+all/any 切换+算子/值沿用既有单条件控件）+选项映射编辑器（控制字段选择+cases 行编辑+fallback）；互斥提示（配了 options 隐藏 optionsFrom 入口反之亦然）
- [x] 2.3 `FormRuntime.tsx`：select 动态选项渲染（optionsFrom 解析）；**选项变化且现值不在新选项集→清值不进载荷**；空选项字段按隐藏呈现（同现行隐藏渲染路径）
- [x] 2.4 测试：镜像校验对拍（新例与 api 测试同表）、编辑器交互（加条件/切 mode/配映射）、运行时清值+空选项隐藏、公开提交端到端（含防篡改）；i18n 补键（zh/en）

## 3. 卫生件（主代理）

- [x] 3.1 wire 库密码轮换：ALTER ROLE（cheap3 wire-postgres）→ fd-wire-env `database_url`+`datasets_database_url` 同批更新 → 清单 datasets URL 改 secretKeyRef → 滚 api（消费者仅 api pod）
- [x] 3.2 deploy/README 记录（凭据轮换配方+消费者清单）

## 4. 门禁与发布

- [x] 4.1 api pytest 全绿 / web vitest+tsc 0 / `openspec validate wire-form-linkage-v2 --strict`
- [x] 4.2 双推→快照仓→CI→relay→滚版→ArgoCD

## 5. 冒烟与归档

- [x] 5.1 生产冒烟：构建器造真表单（组条件+省→市映射）→ 公开提交正例入库 + 伪造非选项值 422 + 控制值切换清值；演练表单清理
- [x] 5.2 `openspec archive wire-form-linkage-v2 -y` + `validate --specs` + ROADMAP 方向 B 标注 + 记忆 + 快照仓同步
