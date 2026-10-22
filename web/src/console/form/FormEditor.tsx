// 表单构建器（wire-form-builder 任务 4.3）。
//
// 数据集下拉（本企业列表，显示 tier/行数提示）→ 按数据集实时列勾选生成
// 字段（默认 label=列名、input 按列类型建议：数值列 number、日期列 date、
// 其余 text——列信息以数据集列表响应内嵌 columns 为准）→ 逐字段改
// label/input/required、select 可编辑 options（逗号分隔）→ submitLabel/
// successMessage 可配 → 保存走既有 PUT 版本链（saveArtifactSpec）。
// 保存前结构自检（validateFormSpec——与后端规则同源），错误逐条呈现零请求；
// 未绑数据集保存允许（构建中间态）；未保存离开 confirm + beforeunload 兜底
// （照抄 DashboardEditor 范式）。字段顺序=勾选时数据集列序。
//
// 显隐条件编辑（wire-form-field-linkage）：每字段卡可选配单条件 visibleWhen
// （当字段/算子/值三控件；empty 族免值；一键清除）。无 visibleWhen 的既有
// 字段默认开关关闭、编辑面零变化；草稿允许中间态（如空值），保存自检
// conditionBad/Self/Missing 拦截。
//
// 条件组合与选项联动（wire-form-linkage-v2）：①「按条件组合显示」——组内
// 条件行增删（1..10）+顶部 all/any 切换，条件行控件与单条件共用一套（互
// 斥：配了单条件则组合入口禁用并提示，反之亦然）；②select「选项联动」——
// 控制字段下拉（其他字段）+cases 映射行（控制值→选项组，选项组沿用现行
// 逗号分隔编辑控件）+可选 fallback（与静态 options 互斥，配了静态选项则
// 入口禁用并提示，切入联动即清 options 键——select 恰有其一）。
//
// 高级录入（wire-form-entry-advanced）：③select 第三源「数据集选项」——
// 数据集下拉（本企业列表）+列下拉+可选过滤条件（字面量）+上限（1..500），
// 三源互斥切入即清其余源键；④字段卡「选值回填」（源字段限本表单动态选项
// select+列下拉限引用集列）；⑤字段卡「计算字段」（op 固定 add_days、日期
// 字段下拉限 date、天数=字面量或 number 字段下拉）；⑥spec 级「提交闸」
// 约束编辑器（id/message/when/probe（数据集+过滤（字面或 {field} 引用）/
// expect）增删改，≤10 条/≤10 filters）。草稿允许中间态（空下拉等），保存
// 自检（validateFormSpec 镜像扩展）前端即拦。
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  apiErrorText,
  fetchDatasets,
  saveArtifactSpec,
  type ArtifactDetail,
  type Dataset,
  type DatasetColumn,
} from "../api";
import {
  DATASET_FILTER_OPS,
  FIELD_CONDITION_OPS,
  FORM_INPUT_TYPES,
  buildFormEnvelope,
  fieldFromColumn,
  validateFormSpec,
  type DatasetFilter,
  type DatasetFilterOp,
  type DatasetFilterValue,
  type FieldConditionOp,
  type FieldConditionGroupMode,
  type FieldComputed,
  type FieldFillFrom,
  type FieldOptionsFrom,
  type FieldOptionsFromDataset,
  type FieldVisibleWhen,
  type FieldVisibleWhenGroup,
  type FormConstraint,
  type FormField,
  type FormInputType,
  type FormProbeFilter,
  type FormSpecBody,
  type FormSpecErrorCode,
} from "./formSpec";

/** 数据集下拉选项文案：名称 + tier 徽标（响应带才显示）+ 行数。 */
function datasetOptionText(
  t: ReturnType<typeof useTranslation>["t"],
  dataset: Dataset,
): string {
  const tier = (dataset as { storage_tier?: string }).storage_tier;
  const tierText =
    tier === "api" || tier === "cell"
      ? t(`console.datasets.tierBadge.${tier}`)
      : null;
  return tierText
    ? t("console.form.editor.datasetOption", {
        name: dataset.name,
        tier: tierText,
        rows: dataset.row_count,
      })
    : t("console.form.editor.datasetOptionPlain", {
        name: dataset.name,
        rows: dataset.row_count,
      });
}

interface FormEditorDraft {
  title: string;
  body: FormSpecBody;
}

export function FormEditor({
  artifactId,
  initial,
  onSaved,
  onExit,
}: {
  artifactId: number;
  /** 服务端当前 spec 解析出的编辑初态（信封标题 + 表单本体）。 */
  initial: FormEditorDraft;
  onSaved: (detail: ArtifactDetail) => void;
  /** 返回查看态（脏态时编辑器先 confirm）。 */
  onExit: () => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState<FormEditorDraft>(initial);
  // 已保存基线（保存成功后推进——dirty 对比用它而不是 initial prop）。
  const [baseline, setBaseline] = useState<FormEditorDraft>(initial);
  const [saving, setSaving] = useState(false);
  const [savedVersion, setSavedVersion] = useState<number | null>(null);
  const [serverError, setServerError] = useState<string | null>(null);
  const [structureErrors, setStructureErrors] = useState<FormSpecErrorCode[]>([]);

  // 数据集列表：编辑器即数据集选择器，挂载即拉取（可重试）。
  const [datasets, setDatasets] = useState<Dataset[] | null>(null);
  const [datasetsLoading, setDatasetsLoading] = useState(false);
  const [datasetsError, setDatasetsError] = useState<string | null>(null);
  const loadDatasets = useCallback(() => {
    setDatasetsLoading(true);
    setDatasetsError(null);
    fetchDatasets()
      .then((list) => setDatasets(list))
      .catch((err) => {
        setDatasetsError(
          apiErrorText(err, t("console.form.editor.datasetsLoadFailed")),
        );
      })
      .finally(() => setDatasetsLoading(false));
  }, [t]);
  useEffect(() => {
    loadDatasets();
  }, [loadDatasets]);

  const dirty = useMemo(
    () => JSON.stringify(draft) !== JSON.stringify(baseline),
    [draft, baseline],
  );

  // 浏览器级离开兜底（页内返回查看的 confirm 在 requestExit 里）。
  useEffect(() => {
    if (!dirty) return;
    const handler = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [dirty]);

  const selectedDataset =
    datasets?.find((d) => d.id === draft.body.datasetId) ?? null;
  const columns: DatasetColumn[] = selectedDataset?.columns ?? [];
  const columnNames = new Set(columns.map((c) => c.name));
  /** 任意数据集的列清单（选项源/回填/探测约束的列下拉共用）。 */
  const columnsOf = (datasetId: string): DatasetColumn[] =>
    datasets?.find((d) => d.id === datasetId)?.columns ?? [];
  /** 本表单内带数据集选项源的 select 字段（回填源下拉候选）。 */
  const dynamicSelectSources = draft.body.fields.filter(
    (f) => f.input === "select" && f.optionsFromDataset !== undefined,
  );

  // ── 草稿态单一来源（wire-console-resilience D5 同构修）：body 变更一律
  // 走 setDraft 函数式更新（从 prev 派生），绝不读渲染闭包里的 draft 快照
  // ——事件连发时闭包里是上一次提交的旧本体，展开回写会清掉前一步选择。
  const patchDraft = (patch: Partial<FormEditorDraft>) => {
    setDraft((prev) => ({ ...prev, ...patch }));
    setSavedVersion(null);
    setServerError(null);
    setStructureErrors([]);
  };

  const patchBody = (patch: Partial<FormSpecBody>) => {
    setDraft((prev) => ({ ...prev, body: { ...prev.body, ...patch } }));
    setSavedVersion(null);
    setServerError(null);
    setStructureErrors([]);
  };

  /** 勾列成字段：新字段插入后按数据集列序稳定重排（不在列清单的字段——
   *  数据集已演进——排在其后并保留，构建器如实标注）。 */
  const toggleColumn = (column: DatasetColumn, checked: boolean) => {
    setDraft((prev) => {
      const fields = checked
        ? sortFields(
            [...prev.body.fields, fieldFromColumn(column)],
            columns,
          )
        : prev.body.fields.filter((f) => f.column !== column.name);
      return { ...prev, body: { ...prev.body, fields } };
    });
    setSavedVersion(null);
    setServerError(null);
    setStructureErrors([]);
  };

  const patchField = (column: string, patch: Partial<FormField>) => {
    setDraft((prev) => ({
      ...prev,
      body: {
        ...prev.body,
        fields: prev.body.fields.map((f) =>
          f.column === column ? { ...f, ...patch } : f,
        ),
      },
    }));
    setSavedVersion(null);
    setServerError(null);
    setStructureErrors([]);
  };

  /** 显隐条件写入/清除（wire-form-field-linkage）：cond=null 时 visibleWhen
   *  键整体不落（既有无条件字段零变化）。 */
  const setFieldCondition = (column: string, cond: FieldVisibleWhen | null) => {
    setDraft((prev) => ({
      ...prev,
      body: {
        ...prev.body,
        fields: prev.body.fields.map((f) => {
          if (f.column !== column) return f;
          if (!cond) {
            const { visibleWhen: _omit, ...rest } = f;
            return rest;
          }
          return { ...f, visibleWhen: cond };
        }),
      },
    }));
    setSavedVersion(null);
    setServerError(null);
    setStructureErrors([]);
  };

  /** 条件组合写入/清除（wire-form-linkage-v2）：group=null 时
   *  visibleWhenGroup 键整体不落；编辑器保证与单条件互斥（入口互禁）。 */
  const setFieldConditionGroup = (
    column: string,
    group: FieldVisibleWhenGroup | null,
  ) => {
    setDraft((prev) => ({
      ...prev,
      body: {
        ...prev.body,
        fields: prev.body.fields.map((f) => {
          if (f.column !== column) return f;
          if (!group) {
            const { visibleWhenGroup: _omit, ...rest } = f;
            return rest;
          }
          return { ...f, visibleWhenGroup: group };
        }),
      },
    }));
    setSavedVersion(null);
    setServerError(null);
    setStructureErrors([]);
  };

  /** 选项联动写入/清除（wire-form-linkage-v2）：source=null 时 optionsFrom
   *  键整体不落；切入联动即清静态 options 键（select 恰有其一，保存校验
   *  optionsExclusive 兜底）。 */
  const setFieldOptionsFrom = (
    column: string,
    source: FieldOptionsFrom | null,
  ) => {
    setDraft((prev) => ({
      ...prev,
      body: {
        ...prev.body,
        fields: prev.body.fields.map((f) => {
          if (f.column !== column) return f;
          if (!source) {
            const { optionsFrom: _omit, ...rest } = f;
            return rest;
          }
          const { options: _options, ...rest } = f;
          return { ...rest, optionsFrom: source };
        }),
      },
    }));
    setSavedVersion(null);
    setServerError(null);
    setStructureErrors([]);
  };

  /** 数据集选项源写入/清除（wire-form-entry-advanced）：source=null 时
   *  optionsFromDataset 键整体不落；切入即清另两源键（三源恰有其一，保存
   *  校验 optionsExclusive 兜底）。 */
  const setFieldOptionsFromDataset = (
    column: string,
    source: FieldOptionsFromDataset | null,
  ) => {
    setDraft((prev) => ({
      ...prev,
      body: {
        ...prev.body,
        fields: prev.body.fields.map((f) => {
          if (f.column !== column) return f;
          if (!source) {
            const { optionsFromDataset: _omit, ...rest } = f;
            return rest;
          }
          const {
            options: _options,
            optionsFrom: _linked,
            ...rest
          } = f;
          return { ...rest, optionsFromDataset: source };
        }),
      },
    }));
    setSavedVersion(null);
    setServerError(null);
    setStructureErrors([]);
  };

  /** 选值回填写入/清除（wire-form-entry-advanced）：fill=null 时 fillFrom
   *  键整体不落。 */
  const setFieldFillFrom = (column: string, fill: FieldFillFrom | null) => {
    setDraft((prev) => ({
      ...prev,
      body: {
        ...prev.body,
        fields: prev.body.fields.map((f) => {
          if (f.column !== column) return f;
          if (!fill) {
            const { fillFrom: _omit, ...rest } = f;
            return rest;
          }
          return { ...f, fillFrom: fill };
        }),
      },
    }));
    setSavedVersion(null);
    setServerError(null);
    setStructureErrors([]);
  };

  /** 计算字段写入/清除（wire-form-entry-advanced）：rule=null 时 computed
   *  键整体不落。 */
  const setFieldComputed = (column: string, rule: FieldComputed | null) => {
    setDraft((prev) => ({
      ...prev,
      body: {
        ...prev.body,
        fields: prev.body.fields.map((f) => {
          if (f.column !== column) return f;
          if (!rule) {
            const { computed: _omit, ...rest } = f;
            return rest;
          }
          return { ...f, computed: rule };
        }),
      },
    }));
    setSavedVersion(null);
    setServerError(null);
    setStructureErrors([]);
  };

  /** 提交闸约束列表写入/清除（wire-form-entry-advanced）：null=整体键不落。 */
  const setSpecConstraints = (next: FormConstraint[] | null) => {
    setDraft((prev) => {
      if (next == null) {
        const { constraints: _omit, ...rest } = prev.body;
        return { ...prev, body: rest };
      }
      return { ...prev, body: { ...prev.body, constraints: next } };
    });
    setSavedVersion(null);
    setServerError(null);
    setStructureErrors([]);
  };

  /** 保存：结构自检（零请求拦截）→ PUT 版本链；datasetId 为空的构建中间态
   *  允许保存（服务端只裁 datasetId 非空时的存在性/属主）。 */
  const save = async () => {
    if (saving) return;
    const title = draft.title.trim();
    const errors = validateFormSpec(title, draft.body);
    if (errors.length > 0) {
      setStructureErrors(errors);
      setSavedVersion(null);
      return;
    }
    setSaving(true);
    setServerError(null);
    setStructureErrors([]);
    try {
      const detail = await saveArtifactSpec(
        artifactId,
        buildFormEnvelope(title, draft.body) as unknown as Record<string, unknown>,
      );
      setSavedVersion(detail.version);
      const saved: FormEditorDraft = {
        title,
        body: buildFormEnvelope(title, draft.body).spec,
      };
      setBaseline(JSON.parse(JSON.stringify(saved)) as FormEditorDraft);
      setDraft(saved);
      onSaved(detail);
    } catch (err) {
      setServerError(apiErrorText(err, t("console.form.editor.saveFailed")));
    } finally {
      setSaving(false);
    }
  };

  const requestExit = () => {
    if (
      dirty &&
      !window.confirm(t("console.form.editor.dirtyLeaveConfirm"))
    ) {
      return;
    }
    onExit();
  };

  return (
    <div data-testid="form-editor" className="mt-2">
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor="form-editor-title" className="text-xs text-slate-500">
          {t("console.form.editor.titleLabel")}
        </label>
        <input
          id="form-editor-title"
          data-testid="editor-form-title"
          value={draft.title}
          onChange={(e) => patchDraft({ title: e.target.value })}
          className="w-56 rounded-md border border-slate-300 px-2 py-1 text-sm"
        />
        <div className="ml-auto flex gap-2">
          <button
            type="button"
            data-testid="editor-form-exit"
            onClick={requestExit}
            className="rounded border border-slate-300 bg-white px-3 py-1 text-xs text-slate-600 hover:bg-slate-100"
          >
            {t("console.form.editor.backToView")}
          </button>
          <button
            type="button"
            data-testid="editor-form-save"
            disabled={saving || !draft.title.trim()}
            onClick={() => void save()}
            className="rounded bg-slate-900 px-3 py-1 text-xs text-white hover:bg-slate-700 disabled:opacity-50"
          >
            {saving
              ? t("console.form.editor.saving")
              : t("console.form.editor.save")}
          </button>
        </div>
      </div>
      {savedVersion != null && (
        <p data-testid="editor-form-saved-version" className="mt-1 text-[11px] text-emerald-600">
          {t("console.form.editor.savedVersion", { version: savedVersion })}
        </p>
      )}
      {serverError && (
        <p data-testid="editor-form-server-error" className="mt-1 text-[11px] text-red-600">
          {serverError}
        </p>
      )}
      {structureErrors.length > 0 && (
        <ul data-testid="editor-form-errors" className="mt-1 space-y-0.5">
          {structureErrors.map((code) => (
            <li
              key={code}
              data-testid="editor-form-error"
              data-code={code}
              className="text-[11px] text-red-600"
            >
              {t(`console.form.editor.errors.${code}`)}
            </li>
          ))}
        </ul>
      )}

      <div className="mt-3 grid grid-cols-1 gap-3 lg:grid-cols-2">
        <div className="rounded-md border border-slate-200 bg-white p-3">
          <div className="flex flex-wrap items-center gap-2">
            <label htmlFor="editor-form-dataset" className="text-xs text-slate-500">
              {t("console.form.editor.datasetLabel")}
            </label>
            <select
              id="editor-form-dataset"
              data-testid="editor-form-dataset"
              value={draft.body.datasetId ?? ""}
              onChange={(e) => {
                // 换数据集：字段保留但按新列清单如实标注缺失列（不静默删）。
                patchBody({ datasetId: e.target.value || null });
              }}
              className="rounded border border-slate-300 px-2 py-1 text-sm"
            >
              <option value="">{t("console.form.editor.datasetPlaceholder")}</option>
              {(datasets ?? []).map((dataset) => (
                <option key={dataset.id} value={dataset.id}>
                  {datasetOptionText(t, dataset)}
                </option>
              ))}
              {/* 已绑数据集不在列表（已删/他企）——保留占位选项，值不漂移。 */}
              {draft.body.datasetId && !selectedDataset && (
                <option value={draft.body.datasetId}>
                  {draft.body.datasetId}
                </option>
              )}
            </select>
            {datasetsLoading && (
              <span className="text-[11px] text-slate-400">
                {t("console.form.editor.loadingDatasets")}
              </span>
            )}
            {datasetsError && (
              <button
                type="button"
                data-testid="editor-form-datasets-retry"
                onClick={loadDatasets}
                className="rounded border border-slate-300 px-2 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100"
              >
                {t("console.form.editor.retry")}
              </button>
            )}
          </div>
          {datasetsError && (
            <p className="mt-1 text-[11px] text-red-600">{datasetsError}</p>
          )}

          {columns.length > 0 && (
            <div className="mt-2">
              <span className="text-[11px] text-slate-400">
                {t("console.form.editor.columnsLabel")}
              </span>
              <span className="ml-1 text-[10px] text-slate-300">
                {t("console.form.editor.columnsHint")}
              </span>
              <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1">
                {columns.map((column) => (
                  <label
                    key={column.name}
                    className="flex items-center gap-1 text-[11px] text-slate-600"
                  >
                    <input
                      type="checkbox"
                      data-testid="editor-form-column"
                      data-column={column.name}
                      checked={draft.body.fields.some(
                        (f) => f.column === column.name,
                      )}
                      onChange={(e) => toggleColumn(column, e.target.checked)}
                    />
                    {column.name}
                    <span className="text-[10px] text-slate-300">{column.type}</span>
                  </label>
                ))}
              </div>
            </div>
          )}

          <div className="mt-3 space-y-1.5">
            <div className="flex flex-wrap items-center gap-2">
              <label htmlFor="editor-form-submit-label" className="text-[11px] text-slate-400">
                {t("console.form.editor.submitLabelLabel")}
              </label>
              <input
                id="editor-form-submit-label"
                data-testid="editor-form-submit-label"
                value={draft.body.submitLabel ?? ""}
                placeholder={t("console.form.editor.submitLabelPlaceholder")}
                onChange={(e) =>
                  patchBody({ submitLabel: e.target.value })
                }
                className="w-40 rounded border border-slate-300 px-2 py-1 text-xs"
              />
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <label htmlFor="editor-form-success-message" className="text-[11px] text-slate-400">
                {t("console.form.editor.successMessageLabel")}
              </label>
              <input
                id="editor-form-success-message"
                data-testid="editor-form-success-message"
                value={draft.body.successMessage ?? ""}
                placeholder={t("console.form.editor.successMessagePlaceholder")}
                onChange={(e) =>
                  patchBody({ successMessage: e.target.value })
                }
                className="w-64 rounded border border-slate-300 px-2 py-1 text-xs"
              />
            </div>
          </div>
        </div>

        {/* 字段卡列表：label/input/required/select options 逐字段可配。 */}
        <div className="rounded-md border border-slate-200 bg-white p-3">
          <h3 className="text-xs font-semibold text-slate-600">
            {t("console.form.editor.columnsLabel")}
          </h3>
          {draft.body.fields.length === 0 && (
            <p data-testid="editor-form-empty-fields" className="mt-2 text-xs text-slate-400">
              {t("console.form.editor.columnsHint")}
            </p>
          )}
          <ul data-testid="editor-field-list" className="mt-2 space-y-2">
            {draft.body.fields.map((field) => (
              <li
                key={field.column}
                data-testid="editor-field"
                data-column={field.column}
                className="rounded border border-slate-200 p-2.5"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span
                    data-testid="editor-field-column"
                    className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] text-slate-500"
                  >
                    {field.column}
                  </span>
                  {!columnNames.has(field.column) && (
                    <span
                      data-testid="editor-field-missing"
                      title={t("console.form.editor.fieldMissingColumn")}
                      className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] text-amber-700"
                    >
                      {t("console.form.editor.fieldMissingColumn")}
                    </span>
                  )}
                  <label className="ml-auto flex items-center gap-1 text-[11px] text-slate-500">
                    <input
                      type="checkbox"
                      data-testid="editor-field-required"
                      checked={field.required === true}
                      onChange={(e) =>
                        patchField(field.column, { required: e.target.checked })
                      }
                    />
                    {t("console.form.editor.fieldRequiredLabel")}
                  </label>
                </div>
                <div className="mt-1.5 flex flex-wrap items-center gap-2">
                  <label className="text-[11px] text-slate-400">
                    {t("console.form.editor.fieldLabelLabel")}
                  </label>
                  <input
                    data-testid="editor-field-label"
                    value={field.label}
                    onChange={(e) =>
                      patchField(field.column, { label: e.target.value })
                    }
                    className="w-36 rounded border border-slate-300 px-2 py-1 text-xs"
                  />
                  <label className="text-[11px] text-slate-400">
                    {t("console.form.editor.fieldInputLabel")}
                  </label>
                  <select
                    data-testid="editor-field-input"
                    value={field.input}
                    onChange={(e) => {
                      const input = e.target.value as FormInputType;
                      patchField(field.column, {
                        input,
                        // select 切入时给一组空 options 占位（保存前必须配齐；
                        // 已配选项联动/数据集源则不加——select 恰有其一）。
                        ...(input === "select" &&
                        !field.options &&
                        !field.optionsFrom &&
                        !field.optionsFromDataset
                          ? { options: [] }
                          : {}),
                      });
                    }}
                    className="rounded border border-slate-300 px-1.5 py-1 text-xs"
                  >
                    {FORM_INPUT_TYPES.map((type) => (
                      <option key={type} value={type}>
                        {t(`console.form.editor.inputTypes.${type}`)}
                      </option>
                    ))}
                  </select>
                </div>
                {/* select 选项源（wire-form-linkage-v2 + wire-form-entry-advanced）：
                    静态 options / 选项联动 optionsFrom / 数据集源
                    optionsFromDataset 三源互斥恰有其一——配了静态选项则联动
                    与数据集入口禁用并提示；切入任一动态源即清其余源键（编辑
                    器保证单源，保存校验 optionsExclusive 兜底）。 */}
                {field.input === "select" &&
                  !field.optionsFrom &&
                  !field.optionsFromDataset && (
                  <div className="mt-1.5 flex flex-wrap items-center gap-2">
                    <label className="text-[11px] text-slate-400">
                      {t("console.form.editor.fieldOptionsLabel")}
                    </label>
                    <input
                      data-testid="editor-field-options"
                      value={(field.options ?? []).join(", ")}
                      onChange={(e) =>
                        patchField(field.column, {
                          options: e.target.value
                            .split(",")
                            .map((option) => option.trim())
                            .filter((option) => option !== ""),
                        })
                      }
                      className="w-64 rounded border border-slate-300 px-2 py-1 text-xs"
                    />
                    <label className="flex items-center gap-1 text-[11px] text-slate-500">
                      <input
                        type="checkbox"
                        data-testid="editor-field-options-from-toggle"
                        disabled={(field.options ?? []).length > 0}
                        onChange={(e) => {
                          if (!e.target.checked) return;
                          const other = draft.body.fields.find(
                            (f) => f.column !== field.column,
                          );
                          setFieldOptionsFrom(field.column, {
                            field: other?.column ?? "",
                            cases: { "": [] },
                          });
                        }}
                      />
                      {t("console.form.editor.optionsFromToggle")}
                    </label>
                    <label className="flex items-center gap-1 text-[11px] text-slate-500">
                      <input
                        type="checkbox"
                        data-testid="editor-field-options-dataset-toggle"
                        disabled={(field.options ?? []).length > 0}
                        onChange={(e) => {
                          if (!e.target.checked) return;
                          setFieldOptionsFromDataset(field.column, {
                            datasetId: "",
                            column: "",
                            filters: [],
                            limit: 200,
                          });
                        }}
                      />
                      {t("console.form.editor.optionsDatasetToggle")}
                    </label>
                    {(field.options ?? []).length > 0 && (
                      <span
                        data-testid="editor-field-options-from-exclusive-hint"
                        className="text-[10px] text-amber-600"
                      >
                        {t("console.form.editor.optionsExclusiveWithStatic")}
                      </span>
                    )}
                  </div>
                )}
                {field.input === "select" && field.optionsFrom && (
                  <div
                    data-testid="editor-field-options-from"
                    className="mt-1.5 space-y-1"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <label className="text-[11px] text-slate-400">
                        {t("console.form.editor.optionsFromFieldLabel")}
                      </label>
                      <select
                        data-testid="editor-field-options-from-field"
                        value={field.optionsFrom.field}
                        onChange={(e) =>
                          setFieldOptionsFrom(field.column, {
                            ...field.optionsFrom!,
                            field: e.target.value,
                          })
                        }
                        className="rounded border border-slate-300 px-1.5 py-1 text-xs"
                      >
                        {(() => {
                          // 候选=同表单其他字段；控制字段已被移除时保留占位
                          // 选项（保存自检 optionsFromMissing 拦截）。
                          const others = draft.body.fields.filter(
                            (f) => f.column !== field.column,
                          );
                          const known = others.some(
                            (f) => f.column === field.optionsFrom?.field,
                          );
                          return (
                            <>
                              {!known && (
                                <option value={field.optionsFrom!.field}>
                                  {field.optionsFrom!.field ||
                                    t("console.form.editor.conditionFieldPlaceholder")}
                                </option>
                              )}
                              {others.map((other) => (
                                <option key={other.column} value={other.column}>
                                  {other.label || other.column}
                                </option>
                              ))}
                            </>
                          );
                        })()}
                      </select>
                      <span
                        data-testid="editor-field-options-exclusive-hint"
                        className="text-[10px] text-slate-400"
                      >
                        {t("console.form.editor.optionsExclusiveWithLinked")}
                      </span>
                      <button
                        type="button"
                        data-testid="editor-field-options-from-clear"
                        onClick={() => setFieldOptionsFrom(field.column, null)}
                        className="rounded border border-slate-300 px-2 py-0.5 text-[10px] text-slate-500 hover:bg-slate-100"
                      >
                        {t("console.form.editor.optionsFromClear")}
                      </button>
                    </div>
                    <ul className="space-y-1">
                      {Object.entries(field.optionsFrom.cases).map(
                        ([caseKey, caseOptions], index) => (
                          <li
                            key={index}
                            data-testid="editor-field-case-row"
                            data-index={index}
                            className="flex flex-wrap items-center gap-2"
                          >
                            <label className="text-[11px] text-slate-400">
                              {t("console.form.editor.optionsFromCaseKeyLabel")}
                            </label>
                            <input
                              data-testid="editor-field-case-key"
                              value={caseKey}
                              onChange={(e) =>
                                setFieldOptionsFrom(
                                  field.column,
                                  renameCaseKey(
                                    field.optionsFrom!,
                                    index,
                                    e.target.value,
                                  ),
                                )
                              }
                              className="w-24 rounded border border-slate-300 px-2 py-1 text-xs"
                            />
                            <label className="text-[11px] text-slate-400">
                              {t("console.form.editor.fieldOptionsLabel")}
                            </label>
                            <input
                              data-testid="editor-field-case-options"
                              value={caseOptions.join(", ")}
                              onChange={(e) =>
                                setFieldOptionsFrom(
                                  field.column,
                                  setCaseOptions(
                                    field.optionsFrom!,
                                    index,
                                    parseOptionListText(e.target.value),
                                  ),
                                )
                              }
                              className="w-48 rounded border border-slate-300 px-2 py-1 text-xs"
                            />
                            <button
                              type="button"
                              data-testid="editor-field-case-remove"
                              disabled={
                                Object.keys(field.optionsFrom!.cases).length <= 1
                              }
                              onClick={() =>
                                setFieldOptionsFrom(
                                  field.column,
                                  removeCase(field.optionsFrom!, index),
                                )
                              }
                              className="rounded border border-slate-300 px-2 py-0.5 text-[10px] text-slate-500 hover:bg-slate-100 disabled:opacity-50"
                            >
                              {t("console.form.editor.optionsFromCaseRemove")}
                            </button>
                          </li>
                        ),
                      )}
                    </ul>
                    <div className="flex flex-wrap items-center gap-2">
                      <button
                        type="button"
                        data-testid="editor-field-case-add"
                        // 映射行为「控制值→选项组」的键不能重复：存在空白键
                        // 行时先配齐该行再加（草稿中间态由保存自检拦截）。
                        disabled={Object.keys(field.optionsFrom.cases).some(
                          (key) => key.trim() === "",
                        )}
                        onClick={() =>
                          setFieldOptionsFrom(field.column, {
                            ...field.optionsFrom!,
                            cases: {
                              ...field.optionsFrom!.cases,
                              "": [],
                            },
                          })
                        }
                        className="rounded border border-slate-300 px-2 py-0.5 text-[10px] text-slate-500 hover:bg-slate-100 disabled:opacity-50"
                      >
                        {t("console.form.editor.optionsFromCaseAdd")}
                      </button>
                      <label className="text-[11px] text-slate-400">
                        {t("console.form.editor.optionsFromFallbackLabel")}
                      </label>
                      <input
                        data-testid="editor-field-options-from-fallback"
                        value={(field.optionsFrom.fallback ?? []).join(", ")}
                        onChange={(e) => {
                          const fallback = parseOptionListText(e.target.value);
                          if (fallback.length === 0) {
                            const { fallback: _omit, ...rest } =
                              field.optionsFrom!;
                            setFieldOptionsFrom(field.column, rest);
                          } else {
                            setFieldOptionsFrom(field.column, {
                              ...field.optionsFrom!,
                              fallback,
                            });
                          }
                        }}
                        className="w-48 rounded border border-slate-300 px-2 py-1 text-xs"
                      />
                    </div>
                  </div>
                )}
                {/* 数据集选项源（wire-form-entry-advanced 4.1）：select 第三源
                    optionsFromDataset——数据集下拉（本企业列表）+列下拉（该集
                    列快照）+可选过滤条件与上限；切入即清另两源键（三源恰有
                    其一）。引用数据集已删/他企时保留占位选项，保存由服务端
                    422 如实裁决。 */}
                {field.input === "select" && field.optionsFromDataset && (
                  <div
                    data-testid="editor-field-options-dataset"
                    className="mt-1.5 space-y-1"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <label className="text-[11px] text-slate-400">
                        {t("console.form.editor.optionsDatasetSourceLabel")}
                      </label>
                      <select
                        data-testid="editor-field-options-dataset-id"
                        value={field.optionsFromDataset.datasetId}
                        onChange={(e) => {
                          // 换数据集：列名随集走——重置 column（不静默保留
                          // 旧集列名）。
                          setFieldOptionsFromDataset(field.column, {
                            ...field.optionsFromDataset!,
                            datasetId: e.target.value,
                            column: "",
                          });
                        }}
                        className="rounded border border-slate-300 px-1.5 py-1 text-xs"
                      >
                        <option value="">
                          {t("console.form.editor.datasetPlaceholder")}
                        </option>
                        {(datasets ?? []).map((dataset) => (
                          <option key={dataset.id} value={dataset.id}>
                            {dataset.name}
                          </option>
                        ))}
                        {field.optionsFromDataset.datasetId &&
                          !datasets?.some(
                            (d) => d.id === field.optionsFromDataset!.datasetId,
                          ) && (
                            <option value={field.optionsFromDataset.datasetId}>
                              {field.optionsFromDataset.datasetId}
                            </option>
                          )}
                      </select>
                      <label className="text-[11px] text-slate-400">
                        {t("console.form.editor.optionsDatasetColumnLabel")}
                      </label>
                      <select
                        data-testid="editor-field-options-dataset-column"
                        value={field.optionsFromDataset.column}
                        onChange={(e) =>
                          setFieldOptionsFromDataset(field.column, {
                            ...field.optionsFromDataset!,
                            column: e.target.value,
                          })
                        }
                        className="rounded border border-slate-300 px-1.5 py-1 text-xs"
                      >
                        <option value="">
                          {t("console.form.editor.optionsDatasetColumnPlaceholder")}
                        </option>
                        {columnsOf(
                          field.optionsFromDataset.datasetId,
                        ).map((col) => (
                          <option key={col.name} value={col.name}>
                            {col.name}
                          </option>
                        ))}
                        {field.optionsFromDataset.column &&
                          !columnsOf(field.optionsFromDataset.datasetId).some(
                            (col) => col.name === field.optionsFromDataset!.column,
                          ) && (
                            <option value={field.optionsFromDataset.column}>
                              {field.optionsFromDataset.column}
                            </option>
                          )}
                      </select>
                      <label className="text-[11px] text-slate-400">
                        {t("console.form.editor.optionsDatasetLimitLabel")}
                      </label>
                      <input
                        data-testid="editor-field-options-dataset-limit"
                        type="number"
                        min={1}
                        max={500}
                        value={field.optionsFromDataset.limit ?? ""}
                        onChange={(e) => {
                          const raw = e.target.value;
                          setFieldOptionsFromDataset(field.column, {
                            ...field.optionsFromDataset!,
                            ...(raw === "" ? {} : { limit: Number(raw) }),
                          });
                        }}
                        className="w-16 rounded border border-slate-300 px-1.5 py-1 text-xs"
                      />
                      <button
                        type="button"
                        data-testid="editor-field-options-dataset-clear"
                        onClick={() =>
                          setFieldOptionsFromDataset(field.column, null)
                        }
                        className="rounded border border-slate-300 px-2 py-0.5 text-[10px] text-slate-500 hover:bg-slate-100"
                      >
                        {t("console.form.editor.optionsDatasetClear")}
                      </button>
                    </div>
                    <ul className="space-y-1">
                      {(field.optionsFromDataset.filters ?? []).map(
                        (filter, index) => (
                          <li
                            key={index}
                            data-testid="editor-field-dataset-filter-row"
                            data-index={index}
                            className="flex flex-wrap items-center gap-2"
                          >
                            <select
                              data-testid="editor-field-dataset-filter-column"
                              value={filter.column}
                              onChange={(e) =>
                                setFieldOptionsFromDataset(field.column, {
                                  ...field.optionsFromDataset!,
                                  filters: (field.optionsFromDataset!.filters ??
                                    []).map((f, i) =>
                                    i === index ? { ...f, column: e.target.value } : f,
                                  ),
                                })
                              }
                              className="rounded border border-slate-300 px-1.5 py-1 text-xs"
                            >
                              <option value="">
                                {t(
                                  "console.form.editor.optionsDatasetColumnPlaceholder",
                                )}
                              </option>
                              {columnsOf(
                                field.optionsFromDataset!.datasetId,
                              ).map((col) => (
                                <option key={col.name} value={col.name}>
                                  {col.name}
                                </option>
                              ))}
                              {filter.column &&
                                !columnsOf(
                                  field.optionsFromDataset!.datasetId,
                                ).some((col) => col.name === filter.column) && (
                                  <option value={filter.column}>
                                    {filter.column}
                                  </option>
                                )}
                            </select>
                            <select
                              data-testid="editor-field-dataset-filter-op"
                              value={filter.op}
                              onChange={(e) =>
                                setFieldOptionsFromDataset(field.column, {
                                  ...field.optionsFromDataset!,
                                  filters: (field.optionsFromDataset!.filters ??
                                    []).map((f, i) =>
                                    i === index
                                      ? {
                                          ...f,
                                          op: e.target
                                            .value as DatasetFilterOp,
                                          value: parseFilterLiteral(
                                            e.target.value as DatasetFilterOp,
                                            filterValueText(f.value),
                                          ),
                                        }
                                      : f,
                                  ),
                                })
                              }
                              className="rounded border border-slate-300 px-1.5 py-1 text-xs"
                            >
                              {DATASET_FILTER_OPS.map((op) => (
                                <option key={op} value={op}>
                                  {t(`console.form.editor.filterOps.${op}`)}
                                </option>
                              ))}
                            </select>
                            <input
                              data-testid="editor-field-dataset-filter-value"
                              value={filterValueText(filter.value)}
                              placeholder={t(
                                "console.form.editor.filterValuePlaceholder",
                              )}
                              onChange={(e) =>
                                setFieldOptionsFromDataset(field.column, {
                                  ...field.optionsFromDataset!,
                                  filters: (field.optionsFromDataset!.filters ??
                                    []).map((f, i) =>
                                    i === index
                                      ? {
                                          ...f,
                                          value: parseFilterLiteral(
                                            f.op,
                                            e.target.value,
                                          ),
                                        }
                                      : f,
                                  ),
                                })
                              }
                              className="w-32 rounded border border-slate-300 px-2 py-1 text-xs"
                            />
                            <button
                              type="button"
                              data-testid="editor-field-dataset-filter-remove"
                              onClick={() =>
                                setFieldOptionsFromDataset(field.column, {
                                  ...field.optionsFromDataset!,
                                  filters: (field.optionsFromDataset!.filters ??
                                    []).filter((_, i) => i !== index),
                                })
                              }
                              className="rounded border border-slate-300 px-2 py-0.5 text-[10px] text-slate-500 hover:bg-slate-100"
                            >
                              {t("console.form.editor.filterRemove")}
                            </button>
                          </li>
                        ),
                      )}
                    </ul>
                    <button
                      type="button"
                      data-testid="editor-field-dataset-filter-add"
                      disabled={
                        (field.optionsFromDataset.filters ?? []).length >= 10
                      }
                      onClick={() =>
                        setFieldOptionsFromDataset(field.column, {
                          ...field.optionsFromDataset!,
                          filters: [
                            ...(field.optionsFromDataset!.filters ?? []),
                            { column: "", op: "eq", value: "" },
                          ],
                        })
                      }
                      className="rounded border border-slate-300 px-2 py-0.5 text-[10px] text-slate-500 hover:bg-slate-100 disabled:opacity-50"
                    >
                      {t("console.form.editor.filterAdd")}
                    </button>
                    <span
                      data-testid="editor-field-options-dataset-exclusive-hint"
                      className="ml-1 text-[10px] text-slate-400"
                    >
                      {t("console.form.editor.optionsExclusiveWithDataset")}
                    </span>
                  </div>
                )}
                {/* 选值回填（wire-form-entry-advanced 4.1）：源字段下拉限本
                    表单带数据集选项源的 select、列下拉限该引用集列；源字段
                    变更即重置回填列（列随集走）。无候选源时入口禁用并提示。 */}
                <div className="mt-1.5 flex flex-wrap items-center gap-2">
                  <label className="flex items-center gap-1 text-[11px] text-slate-500">
                    <input
                      type="checkbox"
                      data-testid="editor-field-fill-from-toggle"
                      checked={field.fillFrom !== undefined}
                      disabled={
                        dynamicSelectSources.filter(
                          (f) => f.column !== field.column,
                        ).length === 0 && field.fillFrom === undefined
                      }
                      onChange={(e) => {
                        if (!e.target.checked) {
                          setFieldFillFrom(field.column, null);
                          return;
                        }
                        const source = dynamicSelectSources.find(
                          (f) => f.column !== field.column,
                        );
                        setFieldFillFrom(field.column, {
                          field: source?.column ?? "",
                          column: "",
                        });
                      }}
                    />
                    {t("console.form.editor.fillFromToggle")}
                  </label>
                  {dynamicSelectSources.filter((f) => f.column !== field.column)
                    .length === 0 &&
                    field.fillFrom === undefined && (
                      <span className="text-[10px] text-slate-300">
                        {t("console.form.editor.fillFromNoSource")}
                      </span>
                    )}
                </div>
                {field.fillFrom && (
                  <div
                    data-testid="editor-field-fill-from"
                    className="mt-1.5 flex flex-wrap items-center gap-2"
                  >
                    <label className="text-[11px] text-slate-400">
                      {t("console.form.editor.fillFromFieldLabel")}
                    </label>
                    <select
                      data-testid="editor-field-fill-from-field"
                      value={field.fillFrom.field}
                      onChange={(e) =>
                        setFieldFillFrom(field.column, {
                          field: e.target.value,
                          column: "",
                        })
                      }
                      className="rounded border border-slate-300 px-1.5 py-1 text-xs"
                    >
                      <option value="">
                        {t("console.form.editor.conditionFieldPlaceholder")}
                      </option>
                      {dynamicSelectSources
                        .filter((f) => f.column !== field.column)
                        .map((source) => (
                          <option key={source.column} value={source.column}>
                            {source.label || source.column}
                          </option>
                        ))}
                      {field.fillFrom.field &&
                        !dynamicSelectSources.some(
                          (f) =>
                            f.column === field.fillFrom!.field &&
                            f.column !== field.column,
                        ) && (
                          <option value={field.fillFrom.field}>
                            {field.fillFrom.field}
                          </option>
                        )}
                    </select>
                    <label className="text-[11px] text-slate-400">
                      {t("console.form.editor.fillFromColumnLabel")}
                    </label>
                    <select
                      data-testid="editor-field-fill-from-column"
                      value={field.fillFrom.column}
                      onChange={(e) =>
                        setFieldFillFrom(field.column, {
                          ...field.fillFrom!,
                          column: e.target.value,
                        })
                      }
                      className="rounded border border-slate-300 px-1.5 py-1 text-xs"
                    >
                      <option value="">
                        {t("console.form.editor.optionsDatasetColumnPlaceholder")}
                      </option>
                      {columnsOf(
                        dynamicSelectSources.find(
                          (f) => f.column === field.fillFrom!.field,
                        )?.optionsFromDataset?.datasetId ?? "",
                      ).map((col) => (
                        <option key={col.name} value={col.name}>
                          {col.name}
                        </option>
                      ))}
                      {field.fillFrom.column &&
                        !columnsOf(
                          dynamicSelectSources.find(
                            (f) => f.column === field.fillFrom!.field,
                          )?.optionsFromDataset?.datasetId ?? "",
                        ).some((col) => col.name === field.fillFrom!.column) && (
                          <option value={field.fillFrom.column}>
                            {field.fillFrom.column}
                          </option>
                        )}
                    </select>
                    <button
                      type="button"
                      data-testid="editor-field-fill-from-clear"
                      onClick={() => setFieldFillFrom(field.column, null)}
                      className="rounded border border-slate-300 px-2 py-0.5 text-[10px] text-slate-500 hover:bg-slate-100"
                    >
                      {t("console.form.editor.fillFromClear")}
                    </button>
                  </div>
                )}
                {/* 计算字段（wire-form-entry-advanced 4.1）：op 固定 add_days、
                    日期字段下拉限本表单 date 字段、天数=字面量输入或本表单
                    number 字段下拉（不得自引用——候选已排除自身）。 */}
                <div className="mt-1.5 flex flex-wrap items-center gap-2">
                  <label className="flex items-center gap-1 text-[11px] text-slate-500">
                    <input
                      type="checkbox"
                      data-testid="editor-field-computed-toggle"
                      checked={field.computed !== undefined}
                      disabled={
                        draft.body.fields.filter(
                          (f) =>
                            f.input === "date" && f.column !== field.column,
                        ).length === 0 && field.computed === undefined
                      }
                      onChange={(e) => {
                        if (!e.target.checked) {
                          setFieldComputed(field.column, null);
                          return;
                        }
                        const dateField = draft.body.fields.find(
                          (f) => f.input === "date" && f.column !== field.column,
                        );
                        setFieldComputed(field.column, {
                          op: "add_days",
                          date: dateField?.column ?? "",
                          days: 0,
                        });
                      }}
                    />
                    {t("console.form.editor.computedToggle")}
                  </label>
                </div>
                {field.computed && (
                  <div
                    data-testid="editor-field-computed"
                    className="mt-1.5 flex flex-wrap items-center gap-2"
                  >
                    <label className="text-[11px] text-slate-400">
                      {t("console.form.editor.computedOpLabel")}
                    </label>
                    <code className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] text-slate-500">
                      add_days
                    </code>
                    <label className="text-[11px] text-slate-400">
                      {t("console.form.editor.computedDateLabel")}
                    </label>
                    <select
                      data-testid="editor-field-computed-date"
                      value={field.computed.date}
                      onChange={(e) =>
                        setFieldComputed(field.column, {
                          ...field.computed!,
                          date: e.target.value,
                        })
                      }
                      className="rounded border border-slate-300 px-1.5 py-1 text-xs"
                    >
                      <option value="">
                        {t("console.form.editor.conditionFieldPlaceholder")}
                      </option>
                      {draft.body.fields
                        .filter(
                          (f) =>
                            f.input === "date" && f.column !== field.column,
                        )
                        .map((f) => (
                          <option key={f.column} value={f.column}>
                            {f.label || f.column}
                          </option>
                        ))}
                      {field.computed.date &&
                        !draft.body.fields.some(
                          (f) =>
                            f.column === field.computed!.date &&
                            f.input === "date" &&
                            f.column !== field.column,
                        ) && (
                          <option value={field.computed.date}>
                            {field.computed.date}
                          </option>
                        )}
                    </select>
                    <label className="text-[11px] text-slate-400">
                      {t("console.form.editor.computedDaysLabel")}
                    </label>
                    <select
                      data-testid="editor-field-computed-days-mode"
                      value={
                        typeof field.computed.days === "number"
                          ? "__literal"
                          : field.computed.days
                      }
                      onChange={(e) =>
                        setFieldComputed(field.column, {
                          ...field.computed!,
                          days:
                            e.target.value === "__literal"
                              ? 0
                              : e.target.value,
                        })
                      }
                      className="rounded border border-slate-300 px-1.5 py-1 text-xs"
                    >
                      <option value="__literal">
                        {t("console.form.editor.computedDaysLiteral")}
                      </option>
                      {draft.body.fields
                        .filter(
                          (f) =>
                            f.input === "number" && f.column !== field.column,
                        )
                        .map((f) => (
                          <option key={f.column} value={f.column}>
                            {f.label || f.column}
                          </option>
                        ))}
                      {typeof field.computed.days === "string" &&
                        field.computed.days !== "" &&
                        !draft.body.fields.some(
                          (f) =>
                            f.column === field.computed!.days &&
                            f.input === "number" &&
                            f.column !== field.column,
                        ) && (
                          <option value={field.computed.days}>
                            {field.computed.days}
                          </option>
                        )}
                    </select>
                    {typeof field.computed.days === "number" && (
                      <input
                        data-testid="editor-field-computed-days-literal"
                        type="number"
                        min={0}
                        step={1}
                        value={field.computed.days}
                        onChange={(e) =>
                          setFieldComputed(field.column, {
                            ...field.computed!,
                            days: Math.max(
                              0,
                              Math.floor(Number(e.target.value) || 0),
                            ),
                          })
                        }
                        className="w-16 rounded border border-slate-300 px-1.5 py-1 text-xs"
                      />
                    )}
                    <button
                      type="button"
                      data-testid="editor-field-computed-clear"
                      onClick={() => setFieldComputed(field.column, null)}
                      className="rounded border border-slate-300 px-2 py-0.5 text-[10px] text-slate-500 hover:bg-slate-100"
                    >
                      {t("console.form.editor.computedClear")}
                    </button>
                  </div>
                )}
                {/* 显隐条件（wire-form-field-linkage + wire-form-linkage-v2）：
                    单条件与条件组合两个入口互斥（配了其一则另一个禁用并提
                    示）；开关默认关（无 visibleWhen/Group 的既有字段零变化），
                    打开即给默认条件（当字段=首个其他字段、算子=等于、值空——
                    草稿允许中间态，保存自检拦截）。 */}
                <div className="mt-1.5 flex flex-wrap items-center gap-2">
                  <label className="flex items-center gap-1 text-[11px] text-slate-500">
                    <input
                      type="checkbox"
                      data-testid="editor-field-condition-toggle"
                      checked={field.visibleWhen !== undefined}
                      disabled={field.visibleWhenGroup !== undefined}
                      onChange={(e) => {
                        if (!e.target.checked) {
                          setFieldCondition(field.column, null);
                          return;
                        }
                        const other = draft.body.fields.find(
                          (f) => f.column !== field.column,
                        );
                        setFieldCondition(field.column, {
                          field: other?.column ?? "",
                          op: "eq",
                          value: "",
                        });
                      }}
                    />
                    {t("console.form.editor.conditionToggle")}
                  </label>
                  {field.visibleWhenGroup !== undefined && (
                    <span
                      data-testid="editor-field-condition-exclusive-hint"
                      className="text-[10px] text-amber-600"
                    >
                      {t("console.form.editor.conditionExclusiveWithGroup")}
                    </span>
                  )}
                  <label className="flex items-center gap-1 text-[11px] text-slate-500">
                    <input
                      type="checkbox"
                      data-testid="editor-field-condition-group-toggle"
                      checked={field.visibleWhenGroup !== undefined}
                      disabled={field.visibleWhen !== undefined}
                      onChange={(e) => {
                        if (!e.target.checked) {
                          setFieldConditionGroup(field.column, null);
                          return;
                        }
                        const other = draft.body.fields.find(
                          (f) => f.column !== field.column,
                        );
                        setFieldConditionGroup(field.column, {
                          mode: "all",
                          conditions: [
                            { field: other?.column ?? "", op: "eq", value: "" },
                          ],
                        });
                      }}
                    />
                    {t("console.form.editor.conditionGroupToggle")}
                  </label>
                  {field.visibleWhen !== undefined && (
                    <span
                      data-testid="editor-field-group-exclusive-hint"
                      className="text-[10px] text-amber-600"
                    >
                      {t("console.form.editor.conditionExclusiveWithSingle")}
                    </span>
                  )}
                </div>
                {field.visibleWhen && (
                  <div
                    data-testid="editor-field-condition"
                    className="mt-1.5 flex flex-wrap items-center gap-2"
                  >
                    <ConditionControls
                      testIdPrefix="editor-field-condition"
                      cond={field.visibleWhen}
                      fields={draft.body.fields}
                      selfColumn={field.column}
                      onChange={(next) => setFieldCondition(field.column, next)}
                      t={t}
                    />
                    <button
                      type="button"
                      data-testid="editor-field-condition-clear"
                      onClick={() => setFieldCondition(field.column, null)}
                      className="rounded border border-slate-300 px-2 py-0.5 text-[10px] text-slate-500 hover:bg-slate-100"
                    >
                      {t("console.form.editor.conditionClear")}
                    </button>
                  </div>
                )}
                {/* 条件组合编辑（wire-form-linkage-v2）：顶部 all/any 切换 +
                    条件行增删（1..10，每行=现行单条件控件）。 */}
                {field.visibleWhenGroup && (
                  <div data-testid="editor-field-group" className="mt-1.5">
                    <div className="flex flex-wrap items-center gap-2">
                      <label className="text-[11px] text-slate-400">
                        {t("console.form.editor.conditionGroupModeLabel")}
                      </label>
                      <select
                        data-testid="editor-field-group-mode"
                        value={field.visibleWhenGroup.mode}
                        onChange={(e) =>
                          setFieldConditionGroup(field.column, {
                            ...field.visibleWhenGroup!,
                            mode: e.target.value as FieldConditionGroupMode,
                          })
                        }
                        className="rounded border border-slate-300 px-1.5 py-1 text-xs"
                      >
                        <option value="all">
                          {t("console.form.editor.conditionGroupModes.all")}
                        </option>
                        <option value="any">
                          {t("console.form.editor.conditionGroupModes.any")}
                        </option>
                      </select>
                      <button
                        type="button"
                        data-testid="editor-field-group-condition-add"
                        disabled={field.visibleWhenGroup.conditions.length >= 10}
                        onClick={() => {
                          const other = draft.body.fields.find(
                            (f) => f.column !== field.column,
                          );
                          setFieldConditionGroup(field.column, {
                            ...field.visibleWhenGroup!,
                            conditions: [
                              ...field.visibleWhenGroup!.conditions,
                              { field: other?.column ?? "", op: "eq", value: "" },
                            ],
                          });
                        }}
                        className="rounded border border-slate-300 px-2 py-0.5 text-[10px] text-slate-500 hover:bg-slate-100 disabled:opacity-50"
                      >
                        {t("console.form.editor.conditionGroupAdd")}
                      </button>
                      <button
                        type="button"
                        data-testid="editor-field-group-clear"
                        onClick={() => setFieldConditionGroup(field.column, null)}
                        className="rounded border border-slate-300 px-2 py-0.5 text-[10px] text-slate-500 hover:bg-slate-100"
                      >
                        {t("console.form.editor.conditionGroupClear")}
                      </button>
                    </div>
                    <ul className="mt-1 space-y-1">
                      {field.visibleWhenGroup.conditions.map((cond, index) => (
                        <li
                          key={index}
                          data-testid="editor-group-condition"
                          data-index={index}
                          className="flex flex-wrap items-center gap-2"
                        >
                          <ConditionControls
                            testIdPrefix="editor-group-condition"
                            cond={cond}
                            fields={draft.body.fields}
                            selfColumn={field.column}
                            onChange={(next) =>
                              setFieldConditionGroup(field.column, {
                                ...field.visibleWhenGroup!,
                                conditions:
                                  field.visibleWhenGroup!.conditions.map((c, i) =>
                                    i === index ? next : c,
                                  ),
                              })
                            }
                            t={t}
                          />
                          <button
                            type="button"
                            data-testid="editor-group-condition-remove"
                            disabled={
                              field.visibleWhenGroup!.conditions.length <= 1
                            }
                            onClick={() =>
                              setFieldConditionGroup(field.column, {
                                ...field.visibleWhenGroup!,
                                conditions:
                                  field.visibleWhenGroup!.conditions.filter(
                                    (_, i) => i !== index,
                                  ),
                              })
                            }
                            className="rounded border border-slate-300 px-2 py-0.5 text-[10px] text-slate-500 hover:bg-slate-100 disabled:opacity-50"
                          >
                            {t("console.form.editor.conditionGroupRemove")}
                          </button>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </li>
            ))}
          </ul>
        </div>
      </div>

      {/* 提交闸约束编辑器（wire-form-entry-advanced 4.2）：spec 级
          constraints 列表增删改——id/message/when（可选单条件）/probe
          （数据集+过滤条件（值=字面或 {field} 引用）+expect none|any）；
          ≤10 条/≤10 filters，保存自检 constraintBad/IdDuplicate 前端即拦。 */}
      <div className="mt-3 rounded-md border border-slate-200 bg-white p-3">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-xs font-semibold text-slate-600">
            {t("console.form.editor.constraintsTitle")}
          </h3>
          <span className="text-[10px] text-slate-300">
            {t("console.form.editor.constraintsHint")}
          </span>
          <button
            type="button"
            data-testid="editor-constraint-add"
            disabled={(draft.body.constraints ?? []).length >= 10}
            onClick={() =>
              setSpecConstraints([
                ...(draft.body.constraints ?? []),
                {
                  id: "",
                  message: "",
                  probe: {
                    datasetId: "",
                    filters: [{ column: "", op: "eq", value: "" }],
                    expect: "none",
                  },
                },
              ])
            }
            className="rounded border border-slate-300 px-2 py-0.5 text-[10px] text-slate-500 hover:bg-slate-100 disabled:opacity-50"
          >
            {t("console.form.editor.constraintAdd")}
          </button>
        </div>
        {(draft.body.constraints ?? []).length === 0 && (
          <p data-testid="editor-constraints-empty" className="mt-2 text-xs text-slate-400">
            {t("console.form.editor.constraintsEmpty")}
          </p>
        )}
        <ul className="mt-2 space-y-2">
          {(draft.body.constraints ?? []).map((constraint, index) => (
            <li
              key={index}
              data-testid="editor-constraint"
              data-index={index}
              className="space-y-1.5 rounded border border-slate-200 p-2.5"
            >
              <div className="flex flex-wrap items-center gap-2">
                <label className="text-[11px] text-slate-400">
                  {t("console.form.editor.constraintIdLabel")}
                </label>
                <input
                  data-testid="editor-constraint-id"
                  value={constraint.id}
                  onChange={(e) =>
                    setSpecConstraints(
                      (draft.body.constraints ?? []).map((c, i) =>
                        i === index ? { ...c, id: e.target.value } : c,
                      ),
                    )
                  }
                  className="w-28 rounded border border-slate-300 px-2 py-1 text-xs"
                />
                <label className="text-[11px] text-slate-400">
                  {t("console.form.editor.constraintMessageLabel")}
                </label>
                <input
                  data-testid="editor-constraint-message"
                  value={constraint.message}
                  onChange={(e) =>
                    setSpecConstraints(
                      (draft.body.constraints ?? []).map((c, i) =>
                        i === index ? { ...c, message: e.target.value } : c,
                      ),
                    )
                  }
                  className="w-64 rounded border border-slate-300 px-2 py-1 text-xs"
                />
                <label className="text-[11px] text-slate-400">
                  {t("console.form.editor.constraintExpectLabel")}
                </label>
                <select
                  data-testid="editor-constraint-expect"
                  value={constraint.probe.expect}
                  onChange={(e) =>
                    setSpecConstraints(
                      (draft.body.constraints ?? []).map((c, i) =>
                        i === index
                          ? {
                              ...c,
                              probe: {
                                ...c.probe,
                                expect: e.target.value as "none" | "any",
                              },
                            }
                          : c,
                      ),
                    )
                  }
                  className="rounded border border-slate-300 px-1.5 py-1 text-xs"
                >
                  <option value="none">
                    {t("console.form.editor.constraintExpects.none")}
                  </option>
                  <option value="any">
                    {t("console.form.editor.constraintExpects.any")}
                  </option>
                </select>
                <button
                  type="button"
                  data-testid="editor-constraint-remove"
                  onClick={() =>
                    setSpecConstraints(
                      (draft.body.constraints ?? []).filter((_, i) => i !== index),
                    )
                  }
                  className="ml-auto rounded border border-slate-300 px-2 py-0.5 text-[10px] text-slate-500 hover:bg-slate-100"
                >
                  {t("console.form.editor.constraintRemove")}
                </button>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <label className="text-[11px] text-slate-400">
                  {t("console.form.editor.constraintProbeDatasetLabel")}
                </label>
                <select
                  data-testid="editor-constraint-probe-dataset"
                  value={constraint.probe.datasetId}
                  onChange={(e) => {
                    // 换数据集：过滤列随集走——列名重置。
                    setSpecConstraints(
                      (draft.body.constraints ?? []).map((c, i) =>
                        i === index
                          ? {
                              ...c,
                              probe: {
                                ...c.probe,
                                datasetId: e.target.value,
                                filters: c.probe.filters.map(() => ({
                                  column: "",
                                  op: "eq" as const,
                                  value: "" as const,
                                })),
                              },
                            }
                          : c,
                      ),
                    );
                  }}
                  className="rounded border border-slate-300 px-1.5 py-1 text-xs"
                >
                  <option value="">
                    {t("console.form.editor.datasetPlaceholder")}
                  </option>
                  {(datasets ?? []).map((dataset) => (
                    <option key={dataset.id} value={dataset.id}>
                      {dataset.name}
                    </option>
                  ))}
                  {constraint.probe.datasetId &&
                    !datasets?.some(
                      (d) => d.id === constraint.probe.datasetId,
                    ) && (
                      <option value={constraint.probe.datasetId}>
                        {constraint.probe.datasetId}
                      </option>
                    )}
                </select>
                <label className="flex items-center gap-1 text-[11px] text-slate-500">
                  <input
                    type="checkbox"
                    data-testid="editor-constraint-when-toggle"
                    checked={constraint.when !== undefined}
                    onChange={(e) => {
                      const others = draft.body.fields;
                      setSpecConstraints(
                        (draft.body.constraints ?? []).map((c, i) => {
                          if (i !== index) return c;
                          if (!e.target.checked) {
                            const { when: _omit, ...rest } = c;
                            return rest;
                          }
                          return {
                            ...c,
                            when: {
                              field: others[0]?.column ?? "",
                              op: "eq",
                              value: "",
                            },
                          };
                        }),
                      );
                    }}
                  />
                  {t("console.form.editor.constraintWhenToggle")}
                </label>
              </div>
              {constraint.when && (
                <div
                  data-testid="editor-constraint-when"
                  className="flex flex-wrap items-center gap-2"
                >
                  <ConditionControls
                    testIdPrefix="editor-constraint-when"
                    cond={constraint.when}
                    fields={draft.body.fields}
                    selfColumn=""
                    onChange={(next) =>
                      setSpecConstraints(
                        (draft.body.constraints ?? []).map((c, i) =>
                          i === index ? { ...c, when: next } : c,
                        ),
                      )
                    }
                    t={t}
                  />
                </div>
              )}
              <ul className="space-y-1">
                {constraint.probe.filters.map((filter, filterIndex) => {
                  const refName = isFieldRefValue(filter.value)
                    ? filter.value.field
                    : "";
                  return (
                  <li
                    key={filterIndex}
                    data-testid="editor-probe-filter-row"
                    data-index={filterIndex}
                    className="flex flex-wrap items-center gap-2"
                  >
                    <select
                      data-testid="editor-probe-filter-column"
                      value={filter.column}
                      onChange={(e) =>
                        setSpecConstraints(
                          (draft.body.constraints ?? []).map((c, i) =>
                            i === index
                              ? {
                                  ...c,
                                  probe: {
                                    ...c.probe,
                                    filters: c.probe.filters.map((f, fi) =>
                                      fi === filterIndex
                                        ? { ...f, column: e.target.value }
                                        : f,
                                    ),
                                  },
                                }
                              : c,
                          ),
                        )
                      }
                      className="rounded border border-slate-300 px-1.5 py-1 text-xs"
                    >
                      <option value="">
                        {t("console.form.editor.optionsDatasetColumnPlaceholder")}
                      </option>
                      {columnsOf(constraint.probe.datasetId).map((col) => (
                        <option key={col.name} value={col.name}>
                          {col.name}
                        </option>
                      ))}
                      {filter.column &&
                        !columnsOf(constraint.probe.datasetId).some(
                          (col) => col.name === filter.column,
                        ) && (
                          <option value={filter.column}>{filter.column}</option>
                        )}
                    </select>
                    <select
                      data-testid="editor-probe-filter-op"
                      value={filter.op}
                      onChange={(e) =>
                        setSpecConstraints(
                          (draft.body.constraints ?? []).map((c, i) =>
                            i === index
                              ? {
                                  ...c,
                                  probe: {
                                    ...c.probe,
                                    filters: c.probe.filters.map((f, fi) =>
                                      fi === filterIndex
                                        ? {
                                            ...f,
                                            op: e.target
                                              .value as DatasetFilterOp,
                                            value: parseFilterLiteral(
                                              e.target.value as DatasetFilterOp,
                                              filterValueText(f.value),
                                            ),
                                          }
                                        : f,
                                    ),
                                  },
                                }
                              : c,
                          ),
                        )
                      }
                      className="rounded border border-slate-300 px-1.5 py-1 text-xs"
                    >
                      {DATASET_FILTER_OPS.map((op) => (
                        <option key={op} value={op}>
                          {t(`console.form.editor.filterOps.${op}`)}
                        </option>
                      ))}
                    </select>
                    {isFieldRefValue(filter.value) ? (
                      <>
                        <select
                          data-testid="editor-probe-filter-value-field"
                          value={filter.value.field}
                          onChange={(e) =>
                            setSpecConstraints(
                              (draft.body.constraints ?? []).map((c, i) =>
                                i === index
                                  ? {
                                      ...c,
                                      probe: {
                                        ...c.probe,
                                        filters: c.probe.filters.map((f, fi) =>
                                          fi === filterIndex
                                            ? {
                                                ...f,
                                                value: { field: e.target.value },
                                              }
                                            : f,
                                        ),
                                      },
                                    }
                                  : c,
                              ),
                            )
                          }
                          className="rounded border border-slate-300 px-1.5 py-1 text-xs"
                        >
                          <option value="">
                            {t("console.form.editor.conditionFieldPlaceholder")}
                          </option>
                          {draft.body.fields.map((f) => (
                            <option key={f.column} value={f.column}>
                              {f.label || f.column}
                            </option>
                          ))}
                          {refName &&
                            !draft.body.fields.some(
                              (f) => f.column === refName,
                            ) && (
                              <option value={refName}>
                                {refName}
                              </option>
                            )}
                        </select>
                        <button
                          type="button"
                          data-testid="editor-probe-filter-value-to-literal"
                          onClick={() =>
                            setSpecConstraints(
                              (draft.body.constraints ?? []).map((c, i) =>
                                i === index
                                  ? {
                                      ...c,
                                      probe: {
                                        ...c.probe,
                                        filters: c.probe.filters.map((f, fi) =>
                                          fi === filterIndex
                                            ? { ...f, value: "" }
                                            : f,
                                        ),
                                      },
                                    }
                                  : c,
                              ),
                            )
                          }
                          className="rounded border border-slate-300 px-2 py-0.5 text-[10px] text-slate-500 hover:bg-slate-100"
                        >
                          {t("console.form.editor.probeValueToLiteral")}
                        </button>
                      </>
                    ) : (
                      <>
                        <input
                          data-testid="editor-probe-filter-value"
                          value={filterValueText(filter.value)}
                          placeholder={t(
                            "console.form.editor.filterValuePlaceholder",
                          )}
                          onChange={(e) =>
                            setSpecConstraints(
                              (draft.body.constraints ?? []).map((c, i) =>
                                i === index
                                  ? {
                                      ...c,
                                      probe: {
                                        ...c.probe,
                                        filters: c.probe.filters.map((f, fi) =>
                                          fi === filterIndex
                                            ? {
                                                ...f,
                                                value: parseFilterLiteral(
                                                  f.op,
                                                  e.target.value,
                                                ),
                                              }
                                            : f,
                                        ),
                                      },
                                    }
                                  : c,
                              ),
                            )
                          }
                          className="w-32 rounded border border-slate-300 px-2 py-1 text-xs"
                        />
                        <button
                          type="button"
                          data-testid="editor-probe-filter-value-to-field"
                          onClick={() =>
                            setSpecConstraints(
                              (draft.body.constraints ?? []).map((c, i) =>
                                i === index
                                  ? {
                                      ...c,
                                      probe: {
                                        ...c.probe,
                                        filters: c.probe.filters.map((f, fi) =>
                                          fi === filterIndex
                                            ? {
                                                ...f,
                                                value: {
                                                  field:
                                                    draft.body.fields[0]
                                                      ?.column ?? "",
                                                },
                                              }
                                            : f,
                                        ),
                                      },
                                    }
                                  : c,
                              ),
                            )
                          }
                          className="rounded border border-slate-300 px-2 py-0.5 text-[10px] text-slate-500 hover:bg-slate-100"
                        >
                          {t("console.form.editor.probeValueToField")}
                        </button>
                      </>
                    )}
                    <button
                      type="button"
                      data-testid="editor-probe-filter-remove"
                      disabled={constraint.probe.filters.length <= 1}
                      onClick={() =>
                        setSpecConstraints(
                          (draft.body.constraints ?? []).map((c, i) =>
                            i === index
                              ? {
                                  ...c,
                                  probe: {
                                    ...c.probe,
                                    filters: c.probe.filters.filter(
                                      (_, fi) => fi !== filterIndex,
                                    ),
                                  },
                                }
                              : c,
                          ),
                        )
                      }
                      className="rounded border border-slate-300 px-2 py-0.5 text-[10px] text-slate-500 hover:bg-slate-100 disabled:opacity-50"
                    >
                      {t("console.form.editor.filterRemove")}
                    </button>
                  </li>
                  );
                })}
              </ul>
              <button
                type="button"
                data-testid="editor-probe-filter-add"
                disabled={constraint.probe.filters.length >= 10}
                onClick={() =>
                  setSpecConstraints(
                    (draft.body.constraints ?? []).map((c, i) =>
                      i === index
                        ? {
                            ...c,
                            probe: {
                              ...c.probe,
                              filters: [
                                ...c.probe.filters,
                                { column: "", op: "eq", value: "" },
                              ],
                            },
                          }
                        : c,
                    ),
                  )
                }
                className="rounded border border-slate-300 px-2 py-0.5 text-[10px] text-slate-500 hover:bg-slate-100 disabled:opacity-50"
              >
                {t("console.form.editor.filterAdd")}
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

/** 字段按数据集列序稳定重排：不在列清单的字段（数据集已演进）排在末尾并
 *  保留原相对顺序——绝不静默丢弃已配字段。 */
function sortFields(
  fields: FormField[],
  columns: DatasetColumn[],
): FormField[] {
  const order = new Map(columns.map((c, i) => [c.name, i]));
  return [...fields].sort(
    (a, b) =>
      (order.get(a.column) ?? columns.length) -
      (order.get(b.column) ?? columns.length),
  );
}

/** 单条件控件三件套（单条件与组条件行共用，wire-form-linkage-v2 抽取）：
 *  当字段下拉（候选=同表单其他字段；引用的字段已被移除时保留占位选项，
 *  值不漂移——保存自检 conditionMissing 如实拦截）+算子下拉（切入 empty 族
 *  →value 键不落；切入带值算子→从空值起步或沿用既有值）+值输入。 */
function ConditionControls({
  testIdPrefix,
  cond,
  fields,
  selfColumn,
  onChange,
  t,
}: {
  testIdPrefix: string;
  cond: FieldVisibleWhen;
  fields: FormField[];
  selfColumn: string;
  onChange: (next: FieldVisibleWhen) => void;
  t: ReturnType<typeof useTranslation>["t"];
}) {
  const others = fields.filter((f) => f.column !== selfColumn);
  const known = others.some((f) => f.column === cond.field);
  return (
    <>
      <label className="text-[11px] text-slate-400">
        {t("console.form.editor.conditionWhenLabel")}
      </label>
      <select
        data-testid={`${testIdPrefix}-field`}
        value={cond.field}
        onChange={(e) => onChange({ ...cond, field: e.target.value })}
        className="rounded border border-slate-300 px-1.5 py-1 text-xs"
      >
        {!known && (
          <option value={cond.field}>
            {cond.field ||
              t("console.form.editor.conditionFieldPlaceholder")}
          </option>
        )}
        {others.map((other) => (
          <option key={other.column} value={other.column}>
            {other.label || other.column}
          </option>
        ))}
      </select>
      <label className="text-[11px] text-slate-400">
        {t("console.form.editor.conditionOpLabel")}
      </label>
      <select
        data-testid={`${testIdPrefix}-op`}
        value={cond.op}
        onChange={(e) => {
          const op = e.target.value as FieldConditionOp;
          const emptyFamily = op === "empty" || op === "notEmpty";
          onChange({
            field: cond.field,
            op,
            ...(emptyFamily
              ? {}
              : {
                  value: cond.value === undefined ? "" : cond.value,
                }),
          });
        }}
        className="rounded border border-slate-300 px-1.5 py-1 text-xs"
      >
        {FIELD_CONDITION_OPS.map((op) => (
          <option key={op} value={op}>
            {t(`console.form.editor.conditionOps.${op}`)}
          </option>
        ))}
      </select>
      {cond.op !== "empty" && cond.op !== "notEmpty" && (
        <>
          <label className="text-[11px] text-slate-400">
            {t("console.form.editor.conditionValueLabel")}
          </label>
          <input
            data-testid={`${testIdPrefix}-value`}
            value={String(cond.value ?? "")}
            onChange={(e) => onChange({ ...cond, value: e.target.value })}
            className="w-28 rounded border border-slate-300 px-2 py-1 text-xs"
          />
        </>
      )}
    </>
  );
}

/** 逗号分隔文本 → 选项数组（trim + 去空；选项联动 cases/fallback 编辑用，
 *  与静态选项输入同款口径）。 */
function parseOptionListText(text: string): string[] {
  return text
    .split(",")
    .map((option) => option.trim())
    .filter((option) => option !== "");
}

/** 过滤值 → 文本（编辑控件呈现）：数组逗号拼接、标量 stringify；
 *  {field} 引用形态不走文本呈现（引用分支用专用下拉）。 */
function filterValueText(value: unknown): string {
  if (Array.isArray(value)) return value.map((v) => String(v)).join(", ");
  if (value && typeof value === "object") return "";
  return String(value ?? "");
}

/** 文本 → 过滤值（字面量解析）：in 算子按逗号拆分数组；数值形态解析为
 *  number（比较族算子需要），其余保持字符串。 */
function parseFilterLiteral(
  op: DatasetFilterOp,
  text: string,
): DatasetFilter["value"] {
  const scalar = (part: string): DatasetFilterValue => {
    const trimmed = part.trim();
    return /^-?\d+(\.\d+)?$/.test(trimmed) ? Number(trimmed) : trimmed;
  };
  if (op === "in") {
    return text
      .split(",")
      .map((part) => part.trim())
      .filter((part) => part !== "")
      .map(scalar);
  }
  return scalar(text);
}

/** probe 过滤值是否为 {field} 引用形态（类型收窄守卫）。 */
function isFieldRefValue(
  value: FormProbeFilter["value"],
): value is { field: string } {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

/** cases 映射行改键（按 entries 序重建对象；草稿允许键重复/空白的中间态，
 *  保存自检 optionsFromBad 拦截）。 */
function renameCaseKey(
  source: FieldOptionsFrom,
  index: number,
  key: string,
): FieldOptionsFrom {
  const entries = Object.entries(source.cases);
  if (entries[index]) entries[index] = [key, entries[index][1]];
  return { ...source, cases: Object.fromEntries(entries) };
}

/** cases 映射行改选项组。 */
function setCaseOptions(
  source: FieldOptionsFrom,
  index: number,
  options: string[],
): FieldOptionsFrom {
  const entries = Object.entries(source.cases);
  if (entries[index]) entries[index] = [entries[index][0], options];
  return { ...source, cases: Object.fromEntries(entries) };
}

/** cases 映射行删除（编辑器至少留一行——cases 键序保持稳定）。 */
function removeCase(source: FieldOptionsFrom, index: number): FieldOptionsFrom {
  const entries = Object.entries(source.cases);
  entries.splice(index, 1);
  return { ...source, cases: Object.fromEntries(entries) };
}
