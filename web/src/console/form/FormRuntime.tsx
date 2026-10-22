// 表单运行时（wire-form-builder 任务 4.2）——console 详情与公开分享页共用。
//
// props {spec, endpoint}：console 态提交走既有 corrections 端点
// （addCorrection op=insert，带登录会话；wire-form-entry-advanced 起携带
// endpoint.artifactId → artifact_id，服务端按该 form 产物当前版本执行全量
// 字段校验+提交闸）；公开态走分享 submit 端点（postPublicJson，免登录、
// 绝不携带 Bearer）。必填/类型前端先拦（validateFormValues——与后端规则
// 同源的纯函数），逐字段错误呈现；成功显示 successMessage（缺省文案
// i18n）；datasetId==null 渲染「未绑定数据集」诚实态（不可提交）。服务端
// 422/429/404 的 detail 如实呈现（提交闸 constraint message 逐字），绝不
// 静默吞掉。
//
// 显隐联动（wire-form-field-linkage）：每次 values 变化重求隐藏集
// （evaluateFieldVisibility），隐藏字段不渲染（连 label 都不出）；字段从
// 可见转隐藏即清其 state 值（重新可见从空开始）；提交走联动感知
// validateFormValues（隐藏必填不拦、隐藏携带值拒）。分享页共用本组件，
// 联动自动生效。
//
// 联动 v1+（wire-form-linkage-v2）：①组条件显隐走同一 evaluateFieldVisibility
// （all/any）；②select 动态选项——optionsFrom 按当前 raw values 解析
// （resolveSelectOptions），解析为空的字段并入隐藏集走现行隐藏渲染路径；
// 解析选项变化且现值不在新选项集 → 立即清值（与隐藏清值同款纪律，提交
// 载荷不含；级联由 setValues 触发的重渲染自然推进）。
//
// 高级录入（wire-form-entry-advanced）：③跨数据集动态选项——
// optionsFromDataset 字段挂载即解析（console 走会话身份 queryDatasetDistinct
// 数据面查询端点，公开页走选项解析端点 fetchPublicJson 绝不带登录头），
// 升序/≤limit/截断如实标注，引用失效呈现可读错误不静默空白（未解析/出错
// 不并入空选项隐藏）；④选值回填——fillFrom 源 select 选值变化即回填默认值
// （console 逐源查 limit=1 行；公开页查选项端点 fills 映射），改选覆盖、
// 可手改、源行缺失留空不报错；⑤计算字段——computed=add_days 参与值（date/
// days）再变才重算覆盖（手改在参与值不变期间保留），求值失败留空；⑥提交
// 闸 constraints 由服务端两路径统一执法（console 带 artifact_id、公开提交
// 内建），前端不预跑 probe——422 的 constraint message 逐字呈现。
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  addCorrection,
  apiErrorText,
  fetchPublicJson,
  postPublicJson,
  queryDataset,
  queryDatasetDistinct,
  type QueryFilter,
  type SharedFormFieldOptions,
} from "../api";
import {
  evaluateComputedField,
  evaluateFieldVisibility,
  lookupSharedFill,
  normalizeDistinctOptions,
  resolveSelectOptions,
  validateFormValues,
  type DynamicOptionsEntry,
  type FormField,
  type FormSpecBody,
} from "./formSpec";

/** 提交目标：console 态（corrections 端点，datasetId 随表单绑定；artifactId
 *  可选——携带则提交带 artifact_id 触发服务端表单校验+提交闸）/ 公开态
 *  （分享 submit 端点完整地址，t/v 已进 query；optionsUrl 为选项解析端点
 *  地址，同样免登录绝不携带 Authorization）。 */
export type FormRuntimeEndpoint =
  | { kind: "console"; datasetId: string | null; artifactId?: number | string }
  | { kind: "public"; url: string; optionsUrl?: string };

export function FormRuntime({
  spec,
  endpoint,
}: {
  spec: FormSpecBody;
  endpoint: FormRuntimeEndpoint;
}) {
  const { t } = useTranslation();
  const [values, setValues] = useState<Record<string, string>>({});
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);
  const [success, setSuccess] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);

  // ── 动态选项（wire-form-entry-advanced 5.1）────────────────────────────
  // optionsFromDataset 字段的运行时解析状态（逐列）；console 走会话身份
  // 数据面查询（distinct），公开页走选项解析端点（一次带回全部字段与
  // fills 回填映射）。
  const dynamicSelects = useMemo(
    () =>
      spec.fields.filter(
        (field): field is FormField & { optionsFromDataset: NonNullable<FormField["optionsFromDataset"]> } =>
          field.input === "select" && field.optionsFromDataset != null,
      ),
    [spec],
  );
  const [dynamicOptions, setDynamicOptions] = useState<
    Record<string, DynamicOptionsEntry>
  >({});
  // 公开选项端点载荷（fills 回填映射查表用；console 路径恒 null）。
  const [sharedOptionFields, setSharedOptionFields] = useState<
    Record<string, SharedFormFieldOptions> | null
  >(null);
  const publicOptionsUrl =
    endpoint.kind === "public" ? endpoint.optionsUrl : undefined;

  useEffect(() => {
    if (dynamicSelects.length === 0) return;
    if (endpoint.kind === "public" && publicOptionsUrl == null) return;
    let alive = true;
    const loading = Object.fromEntries(
      dynamicSelects.map((field) => [
        field.column,
        { status: "loading", options: [], truncated: false } as DynamicOptionsEntry,
      ]),
    );
    setDynamicOptions(loading);
    if (endpoint.kind === "public") {
      fetchPublicJson(publicOptionsUrl!)
        .then((payload) => {
          if (!alive) return;
          const fields =
            (payload as { fields?: Record<string, SharedFormFieldOptions> })
              ?.fields ?? {};
          const next: Record<string, DynamicOptionsEntry> = {};
          for (const field of dynamicSelects) {
            const entry = fields[field.column];
            const limit = field.optionsFromDataset.limit ?? 200;
            if (entry?.error) {
              next[field.column] = {
                status: "error",
                options: [],
                truncated: false,
                error: entry.error,
              };
            } else {
              const { options, truncated } = normalizeDistinctOptions(
                entry?.options ?? [],
                limit,
                entry?.truncated ?? false,
              );
              next[field.column] = { status: "ready", options, truncated };
            }
          }
          setDynamicOptions(next);
          setSharedOptionFields(fields);
        })
        .catch((err: unknown) => {
          if (!alive) return;
          const message = apiErrorText(
            err,
            t("console.form.runtime.optionsLoadFailed"),
          );
          setDynamicOptions(
            Object.fromEntries(
              dynamicSelects.map((field) => [
                field.column,
                {
                  status: "error",
                  options: [],
                  truncated: false,
                  error: message,
                } as DynamicOptionsEntry,
              ]),
            ),
          );
        });
      return () => {
        alive = false;
      };
    }
    // console 路径：逐字段会话身份 distinct 查询（filters+distinct_column）。
    Promise.all(
      dynamicSelects.map(async (field) => {
        const source = field.optionsFromDataset;
        const limit = source.limit ?? 200;
        try {
          const result = await queryDatasetDistinct(source.datasetId, {
            column: source.column,
            limit,
            ...(source.filters && source.filters.length > 0
              ? { filters: source.filters as QueryFilter[] }
              : {}),
          });
          if (!alive) return null;
          const { options, truncated } = normalizeDistinctOptions(
            result?.values ?? [],
            limit,
            result?.truncated ?? false,
          );
          return [
            field.column,
            { status: "ready", options, truncated } as DynamicOptionsEntry,
          ] as const;
        } catch (err) {
          if (!alive) return null;
          return [
            field.column,
            {
              status: "error",
              options: [],
              truncated: false,
              error: apiErrorText(err, t("console.form.runtime.optionsLoadFailed")),
            } as DynamicOptionsEntry,
          ] as const;
        }
      }),
    ).then((entries) => {
      if (!alive) return;
      setDynamicOptions(
        Object.fromEntries(entries.filter((entry) => entry != null)),
      );
    });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dynamicSelects, endpoint.kind, publicOptionsUrl]);

  // 已解析（ready）动态选项表——evaluateFieldVisibility/validateFormValues
  // 的裁决面（loading/error 不并入空选项隐藏：错误态呈现可读错误）。
  const resolvedDynamicOptions = useMemo(() => {
    const map: Record<string, string[]> = {};
    for (const [column, entry] of Object.entries(dynamicOptions)) {
      if (entry.status === "ready") map[column] = entry.options;
    }
    return map;
  }, [dynamicOptions]);

  // 显隐联动：每次 values 变化重求隐藏集（hooks 先于下方 unbound 早退——
  // 组件实例跨 spec 演进保持 hook 顺序稳定）。
  const hiddenColumns = useMemo(
    () => evaluateFieldVisibility(spec.fields, values, resolvedDynamicOptions),
    [spec, values, resolvedDynamicOptions],
  );
  // 稳定依赖键（Set 每次新建；排序串只在隐藏集真正变化时推进 effect）。
  const hiddenKey = useMemo(
    () => [...hiddenColumns].sort().join("\u0000"),
    [hiddenColumns],
  );
  // 字段从可见转隐藏即清其 state 值（隐藏即清，重新可见从空开始——
  // Non-Goal 定案）。级联（隐藏的控制字段被清值→下游可见性变化）由
  // setValues 触发的重渲染自然推进。
  useEffect(() => {
    if (hiddenColumns.size === 0) return;
    setValues((prev) => {
      let changed = false;
      const next = { ...prev };
      for (const column of hiddenColumns) {
        if (next[column]) {
          next[column] = "";
          changed = true;
        }
      }
      return changed ? next : prev;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hiddenKey]);

  // select 动态选项（wire-form-linkage-v2 + wire-form-entry-advanced）：按
  // 当前 raw values 逐 select 解析候选（optionsFrom 命中 cases/回退
  // fallback；静态 options 原样；optionsFromDataset 用运行时解析结果）。
  // 解析为空的字段已由 evaluateFieldVisibility 并入隐藏集（走现行隐藏渲染
  // 与清值路径——不可填即不可见；动态源仅在已解析后参与）。
  const selectOptions = useMemo(() => {
    const map: Record<string, string[]> = {};
    for (const field of spec.fields) {
      if (field.input !== "select") continue;
      map[field.column] = field.optionsFromDataset
        ? (dynamicOptions[field.column]?.options ?? [])
        : resolveSelectOptions(field, values);
    }
    return map;
  }, [spec, values, dynamicOptions]);
  // 稳定依赖键（选项列表真正变化时才推进下方清值 effect）。
  const selectOptionsKey = useMemo(
    () =>
      Object.entries(selectOptions)
        .map(([column, options]) => `${column}:${options.join("\u0001")}`)
        .join("\u0000"),
    [selectOptions],
  );
  // 解析选项变化且现值不在新选项集 → 立即清值（与「隐藏即清」同款纪律：
  // 现值绝不进提交载荷；级联清理由 setValues 触发的重渲染自然推进）。
  useEffect(() => {
    setValues((prev) => {
      let changed = false;
      const next = { ...prev };
      for (const [column, options] of Object.entries(selectOptions)) {
        const current = next[column];
        if (current && !options.includes(current)) {
          next[column] = "";
          changed = true;
        }
      }
      return changed ? next : prev;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectOptionsKey]);

  // ── 选值回填（wire-form-entry-advanced 5.2）────────────────────────────
  // fillFrom 源 select 选值变化（与上次回填时的源值不同）→ 以新默认值覆盖
  // 目标字段（可手改；手改在源值不变期间保留）。console 逐源等值查
  // limit=1 行取声明列；公开页查选项端点 fills 映射；源行缺失/查询失败
  // 留空不报错。
  const lastFillSourceValues = useRef<Record<string, string>>({});
  useEffect(() => {
    const targets = spec.fields.filter((field) => field.fillFrom);
    if (targets.length === 0) return;
    const bySource = new Map<string, typeof targets>();
    for (const field of targets) {
      const sourceColumn = field.fillFrom!.field;
      const list = bySource.get(sourceColumn) ?? [];
      list.push(field);
      bySource.set(sourceColumn, list);
    }
    for (const [sourceColumn, group] of bySource) {
      const sourceValue = String(values[sourceColumn] ?? "").trim();
      if (!sourceValue) continue;
      if (lastFillSourceValues.current[sourceColumn] === sourceValue) continue;
      lastFillSourceValues.current[sourceColumn] = sourceValue;
      if (endpoint.kind === "public") {
        // 公开页：fills 映射查表（选项端点已随选项一并带回）。
        if (!sharedOptionFields) continue;
        const updates: Record<string, string> = {};
        for (const field of group) {
          updates[field.column] =
            lookupSharedFill(
              { fields: sharedOptionFields },
              sourceColumn,
              field.fillFrom!.column,
              sourceValue,
            ) ?? "";
        }
        setValues((prev) => ({ ...prev, ...updates }));
        continue;
      }
      // console：按「选项列=源值」等值查 limit=1 行，取各声明列值回填。
      const source = spec.fields.find(
        (field) => field.column === sourceColumn,
      )?.optionsFromDataset;
      if (!source) continue;
      queryDataset(source.datasetId, {
        filters: [
          { column: source.column, op: "eq", value: sourceValue },
        ],
        limit: 1,
      })
        .then((result) => {
          const row = result?.rows?.[0];
          setValues((prev) => {
            const next = { ...prev };
            for (const field of group) {
              const cell = row?.[field.fillFrom!.column];
              next[field.column] = cell == null ? "" : String(cell).trim();
            }
            return next;
          });
        })
        .catch(() => {
          // 填不到留空不报错（不阻断填写；提交按目标字段类型校验）。
          setValues((prev) => {
            const next = { ...prev };
            for (const field of group) next[field.column] = "";
            return next;
          });
        });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [spec, values, sharedOptionFields, endpoint.kind]);

  // ── 计算字段（wire-form-entry-advanced 5.3）────────────────────────────
  // computed=add_days：参与值（date/days）再变才重算覆盖（含用户手改后的
  // 覆盖——重算即最新默认）；参与值不变期间的手改保留；求值失败留空。
  const lastComputedInputs = useRef<Record<string, string>>({});
  useEffect(() => {
    const computedFields = spec.fields.filter((field) => field.computed);
    if (computedFields.length === 0) return;
    const updates: Record<string, string> = {};
    for (const field of computedFields) {
      const rule = field.computed!;
      const dateRaw = values[rule.date];
      const daysRaw =
        typeof rule.days === "number" ? rule.days : values[rule.days];
      const key = JSON.stringify([dateRaw ?? "", daysRaw ?? ""]);
      if (lastComputedInputs.current[field.column] === key) continue;
      lastComputedInputs.current[field.column] = key;
      updates[field.column] = evaluateComputedField(field, values) ?? "";
    }
    if (Object.keys(updates).length === 0) return;
    setValues((prev) => ({ ...prev, ...updates }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [spec, values]);

  // 未绑定数据集诚实态（构建中间态）：如实呈现而非可提交表单。
  // （局部 const 承接窄化——闭包内保持非空。）
  const boundDatasetId = spec.datasetId;
  if (boundDatasetId == null) {
    return (
      <div
        data-testid="form-runtime-unbound"
        className="rounded-md border border-amber-200 bg-amber-50/60 p-4 text-center"
      >
        <p className="text-sm font-medium text-amber-700">
          {t("console.form.runtime.unboundTitle")}
        </p>
        <p className="mt-1 text-xs text-amber-600">
          {t("console.form.runtime.unboundBody")}
        </p>
      </div>
    );
  }

  const setValue = (column: string, value: string) => {
    setValues((prev) => ({ ...prev, [column]: value }));
    setFieldErrors((prev) => {
      if (!(column in prev)) return prev;
      const next = { ...prev };
      delete next[column];
      return next;
    });
    setSubmitError(null);
  };

  /** 提交：前端先拦（必填/类型/动态选项成员）→ console 走 corrections
   *  （携带 artifactId 时带 artifact_id，服务端执行表单校验+提交闸）/
   *  公开走分享端点（服务端提交闸内建）；成功显示 successMessage 并清空
   *  重填（表单面向重复录入），失败如实呈现（提交闸 constraint message
   *  逐字——服务端 422 detail 原样）。 */
  const submit = async () => {
    if (submitting) return;
    const validation = validateFormValues(spec.fields, values, resolvedDynamicOptions);
    if (Object.keys(validation.errors).length > 0) {
      setFieldErrors(validation.errors);
      setSubmitError(null);
      return;
    }
    setFieldErrors({});
    setSubmitting(true);
    setSubmitError(null);
    try {
      let message: string;
      if (endpoint.kind === "console") {
        await addCorrection(endpoint.datasetId ?? boundDatasetId, {
          op: "insert",
          values: validation.values,
          // wire-form-entry-advanced：携带表单产物 id → 服务端按当前版本
          // 执行全量字段校验+提交闸（不携带行为与现状逐字一致）。
          ...(endpoint.artifactId != null
            ? { artifact_id: String(endpoint.artifactId) }
            : {}),
        });
        message =
          spec.successMessage?.trim() || t("console.form.runtime.successDefault");
      } else {
        const result = (await postPublicJson(endpoint.url, {
          values: validation.values,
        })) as { message?: unknown };
        message =
          (typeof result?.message === "string" && result.message) ||
          spec.successMessage?.trim() ||
          t("console.form.runtime.successDefault");
      }
      setSuccess(message);
      setValues({});
    } catch (err) {
      // 422 字段级原因/提交闸 constraint message、429 限流与行数闸、
      // 404 链接失效：detail 如实呈现（逐字），绝不静默吞掉。
      setSubmitError(
        apiErrorText(err, t("console.form.runtime.submitFailed")),
      );
    } finally {
      setSubmitting(false);
    }
  };

  const submitLabel = spec.submitLabel?.trim() || t("console.form.runtime.submitDefault");

  return (
    <form
      data-testid="form-runtime"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
      className="max-w-xl space-y-3"
    >
      {/* 隐藏字段不渲染（连 label 都不出）；可见性由上方 hiddenColumns 决定。 */}
      {spec.fields
        .filter((field) => !hiddenColumns.has(field.column))
        .map((field) => {
        const error = fieldErrors[field.column];
        const fieldLabel = field.label || field.column;
        const dynamic = dynamicOptions[field.column];
        return (
          <div key={field.column} data-testid="form-field" data-column={field.column}>
            <label
              htmlFor={`form-field-${field.column}`}
              className="flex flex-wrap items-center gap-1 text-xs text-slate-600"
            >
              <span>{fieldLabel}</span>
              {field.required && (
                <span
                  data-testid="form-field-required"
                  title={t("console.form.runtime.requiredMark")}
                  className="rounded bg-rose-100 px-1 text-[10px] text-rose-600"
                >
                  {t("console.form.runtime.requiredMark")}
                </span>
              )}
            </label>
            {field.input === "select" ? (
              <select
                id={`form-field-${field.column}`}
                data-testid="form-field-input"
                value={values[field.column] ?? ""}
                onChange={(e) => setValue(field.column, e.target.value)}
                className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              >
                <option value="">—</option>
                {/* 动态选项（wire-form-linkage-v2 + wire-form-entry-advanced）：
                    按当前 raw values 解析（optionsFrom 命中 cases/回退
                    fallback；静态原样；optionsFromDataset 用运行时解析结果）。 */}
                {(selectOptions[field.column] ?? field.options ?? []).map(
                  (option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ),
                )}
              </select>
            ) : (
              /* 文本输入承载全部类型（number/date 由 validateFormValues 统一校验
                 归一——不依赖浏览器原生控件，规则与后端公开提交同源）。 */
              <input
                id={`form-field-${field.column}`}
                data-testid="form-field-input"
                type="text"
                inputMode={field.input === "number" ? "decimal" : undefined}
                placeholder={field.input === "date" ? "YYYY-MM-DD" : undefined}
                value={values[field.column] ?? ""}
                onChange={(e) => setValue(field.column, e.target.value)}
                className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
            )}
            {/* 动态选项源状态标注（wire-form-entry-advanced）：加载中提示、
                截断如实标注、引用失效可读错误（不静默空白）。 */}
            {dynamic?.status === "loading" && (
              <p data-testid="form-field-options-loading" className="mt-0.5 text-[11px] text-slate-400">
                {t("console.form.runtime.optionsLoading")}
              </p>
            )}
            {dynamic?.status === "ready" && dynamic.truncated && (
              <p data-testid="form-field-options-truncated" className="mt-0.5 text-[11px] text-amber-600">
                {t("console.form.runtime.optionsTruncated", {
                  count: dynamic.options.length,
                })}
              </p>
            )}
            {dynamic?.status === "error" && (
              <p
                data-testid="form-field-options-error"
                className="mt-0.5 text-[11px] text-red-600"
              >
                {dynamic.error ?? t("console.form.runtime.optionsLoadFailed")}
              </p>
            )}
            {error && (
              <p
                data-testid="form-field-error"
                data-column={field.column}
                className="mt-0.5 text-[11px] text-red-600"
              >
                {t(`console.form.runtime.errors.${error}`)}
              </p>
            )}
          </div>
        );
      })}

      <div className="flex flex-wrap items-center gap-3">
        <button
          type="submit"
          data-testid="form-submit"
          disabled={submitting}
          className="rounded bg-slate-900 px-4 py-1.5 text-xs text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {submitting
            ? t("console.form.runtime.submitting")
            : submitLabel}
        </button>
        {success && (
          <span
            data-testid="form-success"
            className="text-xs text-emerald-600"
          >
            {success}
          </span>
        )}
        {submitError && (
          <span data-testid="form-submit-error" className="text-xs text-red-600">
            {submitError}
          </span>
        )}
      </div>
    </form>
  );
}
