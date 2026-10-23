# Tasks: wire-form-field-linkage

- [x] 1. api：form_spec 保存校验放宽（visibleWhen 形状/算子枚举/标量 value/
  引用存在+非自引用，错误定位字段）+ 提交校验联动求值（隐藏必填跳过、
  隐藏携带值 422）+ 同规求值纯函数
- [x] 2. api 测试：校验矩阵（合法单条件/非法形状逐类）+ 求值矩阵（eq/neq/
  数值序/字典序/contains/empty 族/控制字段缺席）+ 提交三态（可见必填拦/
  隐藏必填过/隐藏携带值拒）+ 存量 spec 零回归；全套绿
- [x] 3. web：formSpec.ts（visibleWhen 类型/parse 归一/validate 新错误码/
  evaluateFieldVisibility 纯函数/validateFormValues 联动感知）
- [x] 4. web：FormEditor 显隐条件编辑（当字段/算子/值；empty 族免值；清除
  条件）+ i18n 双语 + FormRuntime 条件渲染（隐藏不渲染、值不进载荷）+ 测试
  （含与 api 求值矩阵同款对拍断言）；tsc/套件绿
- [x] 5. 发布：快照+GHA api/web 镜像+滚版
- [x] 6. 生产冒烟：构建器配条件表单保存→分享/公开提交两分支（显隐往返+
  隐藏必填跳过）+ 存量表单零变化抽查
- [ ] 7. 归档+ROADMAP 方向 B 标注+记忆
