# form-apps Specification

## ADDED Requirements

### Requirement: 多条件组合显隐

字段显隐规则 SHALL 支持多条件组合：可选键 ``visibleWhenGroup`` 为
``{mode: "all"|"any", conditions: [单条件, ...]}``（conditions 1..10 条，每条
即单条件显隐的冻结形状与算子枚举）。保存校验：与 visibleWhen 并存拒（422
定位字段）；组内条件的引用存在性/自引用二趟裁决（前向引用支持同现行）。
求值（双端逐字同规）：all=全部条件真才可见、any=至少一真可见；控制字段值
在提交 raw values 中即参与比较（组内不递归）；任一隐藏路径沿用既有纪律
（隐藏字段携带非空值拒、required 不检查、空值跳过不写键、web 侧清值）。

#### Scenario: all 组合

- **WHEN** 字段带 visibleWhenGroup{mode:"all", conditions:[{eq A}, {gt 金额}]} 且两条件均真
- **THEN** 字段可见可填；任一假即隐藏（携带值被 422 拒）

#### Scenario: any 组合与空组防御

- **WHEN** mode:"any" 且全部条件为假
- **THEN** 字段隐藏；conditions 为空数组或超过 10 条在保存时被 422 拒

#### Scenario: 与单条件互斥

- **WHEN** 字段同时携带 visibleWhen 与 visibleWhenGroup
- **THEN** 保存被拒，文案定位字段与互斥原因

### Requirement: 联动改选项

select 字段 SHALL 支持选项联动：可选键 ``optionsFrom`` 为
``{field, cases: {控制值: [选项...]}, fallback?: [选项...]}`（与静态 options
互斥，select 必须恰有其一；field 引用同表单另一字段，自引用/不存在拒）。
求值（双端逐字同规）：按 raw values 解析控制值（trim 字符串口径）命中 cases
取其列表，未命中/空值取 fallback，无 fallback 为空列表；**解析后选项为空的
字段按隐藏同规**（不可填即不可见）；提交时 select 值必须 ∈ 解析后选项
（公开提交端点以 raw values 重解析，防篡改同规 422）；web 运行时在选项变化
且现值不在新选项集时清值（不进载荷）。

#### Scenario: 级联选项

- **WHEN** select 带 optionsFrom（省→市映射）且控制字段值为命中的省
- **THEN** 运行时呈现该省选项列表；提交属于该列表的值通过、伪造不属于的值被 422 拒

#### Scenario: 空选项按隐藏

- **WHEN** 控制字段未填（无 fallback）→ 解析选项为空
- **THEN** 该字段视同隐藏：携带非空值被拒、required 不检查、空值跳过不写键

#### Scenario: 与静态选项互斥

- **WHEN** select 同时携带 options 与 optionsFrom
- **THEN** 保存被拒，文案定位字段与互斥原因
