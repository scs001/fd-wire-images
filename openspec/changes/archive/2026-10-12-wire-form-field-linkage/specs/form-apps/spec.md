# form-apps Specification（delta：字段联动）

## ADDED Requirements

### Requirement: 字段联动（条件显隐）

form 字段MUST支持可选单条件显隐规则 ``visibleWhen = {field, op, value?}``：
``op`` MUST ∈ ``eq|neq|gt|gte|lt|lte|contains|empty|notEmpty``（empty/notEmpty
不带 value，其余算子 MUST带标量 value——对象/数组拒）；``field`` MUST引用同
表单内**另一字段**的 column（自引用拒）。保存校验（前端体验层+后端裁决面双端
同规）MUST对非法形状如实拒（400/错误码定位到字段）。

求值语义（运行时与提交校验MUST逐字同规）：以当前 values 原样求值，控制字段
缺列/null/纯空白按空值参与；eq/neq/contains 双方 trim 后字符串比较；
gt/gte/lt/lte 双方可解析为有限数则数值比较、否则字典序字符串比较；条件为真
或无 visibleWhen = 可见。运行时隐藏字段MUST不渲染、值不进提交载荷、required
不强制。服务端提交校验MUST用同规求值：隐藏必填跳过校验；隐藏字段在 values
中携带值MUST 422 如实拒（不做静默丢弃）。无 visibleWhen 的存量 spec 行为
MUST零变化。

#### Scenario: 条件显隐往返
- **WHEN** 客户给字段配「当 类型=其他 时显示」，运行时选「其他」
- **THEN** 该字段出现且必填生效；改选其他选项后该字段隐藏、值不提交、
  提交成功（其必填不再拦截）

#### Scenario: 服务端防篡改
- **WHEN** 提交载荷对当前条件判定为隐藏的字段携带了值
- **THEN** 422，文案定位该字段（字段当前不可见，不能携带值）

#### Scenario: 非法条件拒
- **WHEN** visibleWhen 引用不存在的字段/自引用/算子越枚举/该带 value 未带
- **THEN** 保存被拒，错误定位到具体字段

#### Scenario: 存量零回归
- **WHEN** 加载/保存/提交无 visibleWhen 的既有表单
- **THEN** 行为与联动上线前逐字节一致
