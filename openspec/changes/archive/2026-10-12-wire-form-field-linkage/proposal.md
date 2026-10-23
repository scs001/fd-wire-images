# Proposal: wire-form-field-linkage（表单字段联动——条件显隐）

## Why

form-apps v1 的字段全部无条件呈现：客户做「其他（请说明）」「金额>1000 需填审批
号」这类常见表单逻辑时只能拆多个表单或靠提示文案硬扛——简道云/明道云一族的
字段联动是表单产品的基线能力，也是 ROADMAP 方向 B「表单字段联动等 v1+ 深化」
点名的下一件。

## What Changes

给 form spec 的字段增加可选 `visibleWhen`（单条件显隐规则）：

- **spec 形状**：`field.visibleWhen = {field, op, value?}`；`op ∈
  eq|neq|gt|gte|lt|lte|contains|empty|notEmpty`（empty/notEmpty 不带 value，
  其余必带标量 value）；`field` 须引用**同表单另一字段**（自引用拒，400）。
- **求值语义（双端逐字同规）**：以当前 values 原样求值（不做不动点递归——
  控制字段自身隐藏/未填时按空值参与比较，跨字段循环因此无害且确定）：
  - eq/neq/contains：双方 `String(x).trim()` 后字符串比较；
  - gt/gte/lt/lte：双方可解析为有限数则数值比较，否则字典序字符串比较；
  - empty：缺列/null/纯空白；notEmpty 反之；
  - 无 visibleWhen 或条件为真 → 可见。
- **运行时**：隐藏字段不渲染、值不进提交载荷、required 不强制（值随隐藏
  丢弃是显隐的语义而非静默丢数据——提交载荷只含可见字段）。
- **服务端同款求值（防篡改）**：`validate_form_values` 对带 visibleWhen 的
  字段先求值——隐藏必填跳过；**隐藏字段携带值 → 422 如实**（「字段 X 当前
  不可见，不能携带值」）。
- **构建器**：逐字段「显隐条件」编辑（当字段/算子/值三段，empty/notEmpty
  免值；清除条件一键回无条件）。
- **零回归**：无 visibleWhen 的存量 spec 双端行为字节不变；旧版本重放/
  分享页/公开提交照旧。

## Non-Goals

- 多条件组合（and/or 嵌套）——v0 单条件，字段可后续升级为条件数组；
- 联动改 options/改 label/级联清值——只做显隐；
- 隐藏字段值的暂存与恢复（重新可见带回旧值）——隐藏即清，语义最简。

## Impact

- 代码：`api/fd_wire_api/artifacts/form_spec.py`（校验+提交求值）、
  `web/src/console/form/{formSpec.ts,FormEditor.tsx,FormRuntime.tsx}`、
  `web/src/i18n/{zh,en}.ts`；纯函数层零库零引擎依赖不变。
- 兼容：spec 校验只放宽（新增可选键），旧 spec 全通过；产物版本链零迁移。
- spec：form-apps +1 要求（字段联动），既有四要求零改动。
