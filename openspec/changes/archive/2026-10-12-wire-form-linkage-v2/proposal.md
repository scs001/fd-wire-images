# wire-form-linkage-v2

## Why

wire-form-field-linkage 落了单条件显隐（visibleWhen 九算子+隐藏即清值+公开提交
同规防篡改）。方向 B 剩下的深化项即 v1+ 两件（ROADMAP「表单联动 v1+」）：

1. **多条件组合**——现实表单的显隐很少只看一个条件（「类型=A 且 金额>100」
   才显示明细字段）；现在只能单条件，构建者被迫拆字段或放弃联动。
2. **联动改选项**——级联选择（省→市、类别→子类）是表单最常见的数据联动；
   现在 select 选项是静态列表，级联无从表达。

## What Changes

**契约（additive，存量 spec 零迁移零漂移）**：

- **`visibleWhenGroup`（多条件显隐）**：field 可选新键
  `{mode: "all"|"any", conditions: [{field, op, value?}, ...]}`——conditions
  1..10 条，每条即现行单条件形状（同一校验器同一算子枚举）；与 visibleWhen
  互斥（并存拒）；引用存在性/自引用二趟裁决覆盖组内全部条件；求值：
  all=全真可见、any=至少一真可见（组内不递归，控制字段值即参与比较——
  同现行冻结语义）。
- **`optionsFrom`（联动改选项）**：select 字段可选新键
  `{field, cases: {<控制值>: [选项...], ...}, fallback?: [选项...]}`——
  与静态 options 互斥（select 必须恰有其一）；field 引用同表单另一字段
  （自引用/不存在拒）；cases 键=控制字段值的 trim 字符串形态（同
  `_scalar_text` 冻结口径）、值为非空字符串选项列表；按提交 raw values
  解析：控制值命中 cases 取其列表，未命中/空值 → fallback，无 fallback →
  空列表；**解析后选项为空 → 字段按隐藏同规**（不可填即不可见：携带非空
  值拒、required 不检查、空值跳过）；select 值成员校验动态化——值必须 ∈
  解析后选项（公开提交端点用 raw values 重解析，防篡改同规）。

**实现面**：

- **api**（`artifacts/form_spec.py`）：结构校验（group/optionsFrom 形状+
  互斥+引用二趟）；`evaluate_hidden_columns` 扩展（组条件求值+空选项列）；
  `validate_form_values` 的 select 分支动态成员校验。纯函数零依赖纪律不变。
- **web**：`formSpec.ts` 镜像校验+求值（双端对拍扩充）；`FormEditor.tsx`
  组条件编辑器（增删条件行+all/any 切换）与选项映射编辑器（控制值→选项
  组+fallback）；`FormRuntime.tsx` 动态选项渲染+**选项变化时现值不在新
  选项集即清值**（同「隐藏即清值」纪律，不进载荷）。

## Design Decisions

- **additive 而非改形状**：单条件 visibleWhen 与静态 options 原样保留——
  存量表单 spec 零迁移零重存，旧版本只读旧键。
- **空选项=隐藏**：级联里「上级未选」时下级无可选项——此时把 required
  强加给用户是陷阱；与显隐共用「不可填即不可见」语义，规则面只此一条。
- **互斥即校验**：visibleWhen×visibleWhenGroup、options×optionsFrom 并存
  均拒（422 定位字段）——两套并存的求值序会成为暗坑，宁可在保存时钉死。
- **cases 键用字符串形态**：JSON 对象键天然字符串；控制值按 `_scalar_text`
  trim 后匹配，与 visibleWhen 比较口径一致。

## Capabilities

### Modified Capabilities

- `form-apps`: +2 要求（多条件组合显隐；联动改选项与动态成员校验）。

## Impact

- **api**：`artifacts/form_spec.py` + 测试（组/映射校验矩阵、求值对拍新例、
  动态成员防篡改）。
- **web**：`formSpec.ts`/`FormEditor.tsx`/`FormRuntime.tsx` + 测试（镜像校验、
  编辑器交互、运行时清值、对拍）。
- 零迁移、零新表、零 env。

## 搭车卫生件（非本 change 契约内，随本轮发布）

`deploy/k8s/fd-wire-api.yaml` 中 datasets PG 连接串密码明文（10-11 实迁遗留）
迁 secret（fd-wire-env `datasets_database_url`）+ wire 库密码轮换（ALTER ROLE
→ 双 secret 同批 → 滚 api；消费者仅 api pod，轮换窗口秒级）。
