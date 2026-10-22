// 单记录页构建器（wire-record-view 任务 4.1/4.2）。
//
// 主数据集选择（本企业列表，tier/行数提示）→ 键列选择 → headerFields 列
// 勾选与 label 改写 → sections 增删配（标题/数据集/日期列/上限/filters——
// 值可为字面量或 {fromField} 引用主记录字段，两形态一键互切）→ 保存走既有
// PUT 版本链（saveArtifactSpec，信封 {title, spec:{…}}）。保存前草稿校验
// validateRecordViewSpec（与后端保存校验同规的前端镜像）零请求拦截；未绑
// 数据集中间态可保存（不可预览/分享，如实提示）；未保存离开 confirm +
// beforeunload 兜底（照 FormEditor/DashboardEditor 范式）。
//
// 试预览（任务 4.2）：示例键值选择器（主数据集 keyColumn 去重值下拉，上限
// 500，或手输兜底）→ 点「试预览」调 POST /artifacts/{id}/record-view/preview
// （console 会话身份、服务端按当前版本 spec 同一取数组装逻辑）→ 复用
// RecordViewRuntime 渲染与公开扫码页同构的时间轴（不生成公开链接）。
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  apiErrorText,
  fetchDatasets,
  queryDatasetDistinct,
  saveArtifactSpec,
  type ArtifactDetail,
  type Dataset,
  type DatasetColumn,
} from "../api";
import { normalizeDistinctOptions } from "../form/formSpec";
import {
  buildRecordViewEnvelope,
  RECORD_VIEW_FILTER_OPS,
  RECORD_VIEW_LIMITS,
  validateRecordViewSpec,
  type RecordViewFilter,
  type RecordViewFilterOp,
  type RecordViewLiteral,
  type RecordViewSection,
  type RecordViewSpecBody,
  type RecordViewSpecErrorCode,
} from "./recordViewSpec";
import { RecordViewRuntime } from "./RecordViewRuntime";

/** 数据集下拉选项文案：名称 + tier 徽标（响应带才显示）+ 行数（照 FormEditor）。 */
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
    ? t("console.recordview.editor.datasetOption", {
        name: dataset.name,
        tier: tierText,
        rows: dataset.row_count,
      })
    : t("console.recordview.editor.datasetOptionPlain", {
        name: dataset.name,
        rows: dataset.row_count,
      });
}

interface RecordViewEditorDraft {
  title: string;
  body: RecordViewSpecBody;
}

/** 过滤值 → 文本（编辑控件呈现）：数组逗号拼接、标量 stringify；
 *  {fromField} 引用形态不走文本呈现（引用分支用专用下拉）。 */
function filterValueText(value: RecordViewFilter["value"]): string {
  if (Array.isArray(value)) return value.map((v) => String(v)).join(", ");
  if (value && typeof value === "object") return "";
  return String(value ?? "");
}

/** 文本 → 过滤字面值：in 算子按逗号拆数组；数值形态解析 number，其余字符串。 */
function parseFilterLiteral(
  op: RecordViewFilterOp,
  text: string,
): RecordViewLiteral | RecordViewLiteral[] {
  const scalar = (part: string): RecordViewLiteral => {
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

/** 过滤值是否 {fromField} 引用形态（类型收窄守卫）。 */
function isFromFieldValue(
  value: RecordViewFilter["value"],
): value is { fromField: string } {
  return (
    !!value && typeof value === "object" && !Array.isArray(value) && "fromField" in value
  );
}

export function RecordViewEditor({
  artifactId,
  initial,
  onSaved,
  onExit,
}: {
  artifactId: number;
  /** 服务端当前 spec 解析出的编辑初态（信封标题 + 本体）。 */
  initial: RecordViewEditorDraft;
  onSaved: (detail: ArtifactDetail) => void;
  /** 返回查看态（脏态时编辑器先 confirm）。 */
  onExit: () => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState<RecordViewEditorDraft>(initial);
  // 已保存基线（保存成功后推进——dirty 对比用它而不是 initial prop）。
  const [baseline, setBaseline] = useState<RecordViewEditorDraft>(initial);
  const [saving, setSaving] = useState(false);
  const [savedVersion, setSavedVersion] = useState<number | null>(null);
  const [serverError, setServerError] = useState<string | null>(null);
  const [structureErrors, setStructureErrors] = useState<
    RecordViewSpecErrorCode[]
  >([]);

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
          apiErrorText(err, t("console.recordview.editor.datasetsLoadFailed")),
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
  const mainColumns: DatasetColumn[] = selectedDataset?.columns ?? [];
  /** 任意数据集的列清单（section 日期列/过滤列下拉共用）；不在列表返回 null
   *  （已删数据集——校验跳过列成员、由服务端 422 兜底）。 */
  const columnsOf = useCallback(
    (datasetId: string): DatasetColumn[] | null => {
      if (!datasetId) return null;
      const dataset = datasets?.find((d) => d.id === datasetId);
      return dataset ? dataset.columns : null;
    },
    [datasets],
  );

  const patchDraft = (patch: Partial<RecordViewEditorDraft>) => {
    setDraft((prev) => ({ ...prev, ...patch }));
    setSavedVersion(null);
    setServerError(null);
    setStructureErrors([]);
  };

  // ── 草稿态单一来源（wire-console-resilience D5）：body 变更一律走
  // setDraft 函数式更新（从 prev 派生），绝不读渲染闭包里的 draft 快照——
  // 事件连发（真实浏览器/E2E 背靠背派发）时闭包里是上一次提交的旧本体，
  // 展开回写会把前一步未及提交的选择（主数据集/键列/勾选）清掉。
  const patchBody = (patch: Partial<RecordViewSpecBody>) => {
    setDraft((prev) => ({ ...prev, body: { ...prev.body, ...patch } }));
    setSavedVersion(null);
    setServerError(null);
    setStructureErrors([]);
  };

  /** 换主数据集：键列失效清空、headerFields 保留但按新列清单如实标注
   *  （不在清单的保存自检拦截——与 FormEditor「不静默删」同款纪律）。 */
  const setMainDataset = (datasetId: string) => {
    patchBody({ datasetId: datasetId || null, keyColumn: null });
  };

  /** 勾列成头部字段：新字段插入后按数据集列序稳定重排。 */
  const toggleHeaderColumn = (column: DatasetColumn, checked: boolean) => {
    setDraft((prev) => {
      const fields = checked
        ? [...prev.body.headerFields, { column: column.name }]
        : prev.body.headerFields.filter((f) => f.column !== column.name);
      const order = new Map(mainColumns.map((c, i) => [c.name, i]));
      fields.sort(
        (a, b) => (order.get(a.column) ?? 999) - (order.get(b.column) ?? 999),
      );
      return { ...prev, body: { ...prev.body, headerFields: fields } };
    });
    setSavedVersion(null);
    setServerError(null);
    setStructureErrors([]);
  };

  const patchHeaderField = (column: string, label: string) => {
    setDraft((prev) => ({
      ...prev,
      body: {
        ...prev.body,
        headerFields: prev.body.headerFields.map((f) =>
          f.column === column
            ? label.trim()
              ? { ...f, label }
              : { column: f.column }
            : f,
        ),
      },
    }));
    setSavedVersion(null);
    setServerError(null);
    setStructureErrors([]);
  };

  const patchSection = (index: number, patch: Partial<RecordViewSection>) => {
    setDraft((prev) => ({
      ...prev,
      body: {
        ...prev.body,
        sections: prev.body.sections.map((s, i) =>
          i === index ? { ...s, ...patch } : s,
        ),
      },
    }));
    setSavedVersion(null);
    setServerError(null);
    setStructureErrors([]);
  };

  const addSection = () => {
    setDraft((prev) => ({
      ...prev,
      body: {
        ...prev.body,
        sections: [
          ...prev.body.sections,
          {
            title: "",
            datasetId: "",
            dateColumn: "",
            filters: [],
            limit: RECORD_VIEW_LIMITS.limitDefault,
          },
        ],
      },
    }));
    setSavedVersion(null);
    setServerError(null);
    setStructureErrors([]);
  };

  const removeSection = (index: number) => {
    setDraft((prev) => ({
      ...prev,
      body: {
        ...prev.body,
        sections: prev.body.sections.filter((_, i) => i !== index),
      },
    }));
    setSavedVersion(null);
    setServerError(null);
    setStructureErrors([]);
  };

  const patchFilter = (
    sectionIndex: number,
    filterIndex: number,
    patch: Partial<RecordViewFilter>,
  ) => {
    setDraft((prev) => ({
      ...prev,
      body: {
        ...prev.body,
        sections: prev.body.sections.map((s, i) =>
          i === sectionIndex
            ? {
                ...s,
                filters: s.filters.map((f, fi) =>
                  fi === filterIndex ? { ...f, ...patch } : f,
                ),
              }
            : s,
        ),
      },
    }));
    setSavedVersion(null);
    setServerError(null);
    setStructureErrors([]);
  };

  const addFilter = (sectionIndex: number) => {
    setDraft((prev) => ({
      ...prev,
      body: {
        ...prev.body,
        sections: prev.body.sections.map((s, i) =>
          i === sectionIndex
            ? {
                ...s,
                filters: [...s.filters, { column: "", op: "eq", value: "" }],
              }
            : s,
        ),
      },
    }));
    setSavedVersion(null);
    setServerError(null);
    setStructureErrors([]);
  };

  const removeFilter = (sectionIndex: number, filterIndex: number) => {
    setDraft((prev) => ({
      ...prev,
      body: {
        ...prev.body,
        sections: prev.body.sections.map((s, i) =>
          i === sectionIndex
            ? { ...s, filters: s.filters.filter((_, fi) => fi !== filterIndex) }
            : s,
        ),
      },
    }));
    setSavedVersion(null);
    setServerError(null);
    setStructureErrors([]);
  };

  /** 保存：草稿校验（零请求拦截，与后端保存校验同规镜像）→ PUT 版本链；
   *  未绑数据集的最小空 spec 允许保存（构建中间态）。 */
  const save = async () => {
    if (saving) return;
    const title = draft.title.trim();
    const errors = validateRecordViewSpec(title, draft.body, columnsOf);
    if (errors.length > 0) {
      setStructureErrors(errors);
      setSavedVersion(null);
      return;
    }
    setSaving(true);
    setServerError(null);
    setStructureErrors([]);
    try {
      const envelope = buildRecordViewEnvelope(title, draft.body);
      const detail = await saveArtifactSpec(
        artifactId,
        envelope as unknown as Record<string, unknown>,
      );
      setSavedVersion(detail.version);
      const saved: RecordViewEditorDraft = { title, body: envelope.spec };
      setBaseline(JSON.parse(JSON.stringify(saved)) as RecordViewEditorDraft);
      setDraft(saved);
      onSaved(detail);
    } catch (err) {
      setServerError(apiErrorText(err, t("console.recordview.editor.saveFailed")));
    } finally {
      setSaving(false);
    }
  };

  const requestExit = () => {
    if (
      dirty &&
      !window.confirm(t("console.recordview.editor.dirtyLeaveConfirm"))
    ) {
      return;
    }
    onExit();
  };

  // ── 试预览（任务 4.2）：键值选择器 + RecordViewRuntime（console 身份）。 ──
  const previewReady =
    draft.body.datasetId != null && !!draft.body.keyColumn?.trim();
  const [keyOptions, setKeyOptions] = useState<string[] | null>(null);
  const [keyTruncated, setKeyTruncated] = useState(false);
  const [keyLoadError, setKeyLoadError] = useState<string | null>(null);
  const [selectedKey, setSelectedKey] = useState("");
  const [manualKey, setManualKey] = useState("");
  const [previewKey, setPreviewKey] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setKeyOptions(null);
    setKeyTruncated(false);
    setKeyLoadError(null);
    setPreviewKey(null);
    if (!previewReady) return;
    queryDatasetDistinct(draft.body.datasetId!, {
      column: draft.body.keyColumn!,
      limit: RECORD_VIEW_LIMITS.distinctKeys,
    })
      .then((result) => {
        if (!alive) return;
        const { options, truncated } = normalizeDistinctOptions(
          result?.values ?? [],
          RECORD_VIEW_LIMITS.distinctKeys,
          result?.truncated ?? false,
        );
        setKeyOptions(options);
        setKeyTruncated(truncated);
      })
      .catch((err) => {
        if (!alive) return;
        setKeyLoadError(
          apiErrorText(err, t("console.recordview.preview.keysLoadFailed")),
        );
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft.body.datasetId, draft.body.keyColumn, t]);

  /** 试预览键值：手输优先（兜底路径），其次下拉选择。 */
  const effectiveKey = manualKey.trim() || selectedKey;

  const runPreview = () => {
    if (!previewReady || !effectiveKey) return;
    setPreviewKey(effectiveKey);
  };

  return (
    <div data-testid="record-view-editor" className="mt-2">
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor="record-view-editor-title" className="text-xs text-slate-500">
          {t("console.recordview.editor.titleLabel")}
        </label>
        <input
          id="record-view-editor-title"
          data-testid="editor-recordview-title"
          value={draft.title}
          onChange={(e) => patchDraft({ title: e.target.value })}
          className="w-56 rounded-md border border-slate-300 px-2 py-1 text-sm"
        />
        <div className="ml-auto flex gap-2">
          <button
            type="button"
            data-testid="editor-recordview-exit"
            onClick={requestExit}
            className="rounded border border-slate-300 bg-white px-3 py-1 text-xs text-slate-600 hover:bg-slate-100"
          >
            {t("console.recordview.editor.backToView")}
          </button>
          <button
            type="button"
            data-testid="editor-recordview-save"
            disabled={saving || !draft.title.trim()}
            onClick={() => void save()}
            className="rounded bg-slate-900 px-3 py-1 text-xs text-white hover:bg-slate-700 disabled:opacity-50"
          >
            {saving
              ? t("console.recordview.editor.saving")
              : t("console.recordview.editor.save")}
          </button>
        </div>
      </div>
      {savedVersion != null && (
        <p
          data-testid="editor-recordview-saved-version"
          className="mt-1 text-[11px] text-emerald-600"
        >
          {t("console.recordview.editor.savedVersion", { version: savedVersion })}
        </p>
      )}
      {serverError && (
        <p
          data-testid="editor-recordview-server-error"
          className="mt-1 text-[11px] text-red-600"
        >
          {serverError}
        </p>
      )}
      {structureErrors.length > 0 && (
        <ul data-testid="editor-recordview-errors" className="mt-1 space-y-0.5">
          {structureErrors.map((code) => (
            <li
              key={code}
              data-testid="editor-recordview-error"
              data-code={code}
              className="text-[11px] text-red-600"
            >
              {t(`console.recordview.editor.errors.${code}`)}
            </li>
          ))}
        </ul>
      )}

      <div className="mt-3 grid grid-cols-1 gap-3 lg:grid-cols-2">
        {/* 左：主数据集 + 键列 + headerFields 勾选与 label。 */}
        <div className="rounded-md border border-slate-200 bg-white p-3">
          <div className="flex flex-wrap items-center gap-2">
            <label htmlFor="editor-recordview-dataset" className="text-xs text-slate-500">
              {t("console.recordview.editor.datasetLabel")}
            </label>
            <select
              id="editor-recordview-dataset"
              data-testid="editor-recordview-dataset"
              value={draft.body.datasetId ?? ""}
              onChange={(e) => setMainDataset(e.target.value)}
              className="rounded border border-slate-300 px-2 py-1 text-sm"
            >
              <option value="">
                {t("console.recordview.editor.datasetPlaceholder")}
              </option>
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
                {t("console.recordview.editor.loadingDatasets")}
              </span>
            )}
            {datasetsError && (
              <button
                type="button"
                data-testid="editor-recordview-datasets-retry"
                onClick={loadDatasets}
                className="rounded border border-slate-300 px-2 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100"
              >
                {t("console.recordview.editor.retry")}
              </button>
            )}
          </div>
          {datasetsError && (
            <p className="mt-1 text-[11px] text-red-600">{datasetsError}</p>
          )}

          {draft.body.datasetId && (
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <label htmlFor="editor-recordview-key-column" className="text-xs text-slate-500">
                {t("console.recordview.editor.keyColumnLabel")}
              </label>
              <select
                id="editor-recordview-key-column"
                data-testid="editor-recordview-key-column"
                value={draft.body.keyColumn ?? ""}
                onChange={(e) => patchBody({ keyColumn: e.target.value || null })}
                className="rounded border border-slate-300 px-2 py-1 text-sm"
              >
                <option value="">
                  {t("console.recordview.editor.keyColumnPlaceholder")}
                </option>
                {mainColumns.map((column) => (
                  <option key={column.name} value={column.name}>
                    {column.name}
                  </option>
                ))}
                {/* 键列已不在列快照（数据集演进）——保留占位，保存自检拦截。 */}
                {draft.body.keyColumn &&
                  !mainColumns.some((c) => c.name === draft.body.keyColumn) && (
                    <option value={draft.body.keyColumn}>
                      {draft.body.keyColumn}
                    </option>
                  )}
              </select>
              <span className="text-[10px] text-slate-400">
                {t("console.recordview.editor.keyColumnHint")}
              </span>
            </div>
          )}

          {mainColumns.length > 0 && (
            <div className="mt-2">
              <span className="text-[11px] text-slate-400">
                {t("console.recordview.editor.headerFieldsLabel")}
              </span>
              <span className="ml-1 text-[10px] text-slate-300">
                {t("console.recordview.editor.headerFieldsHint")}
              </span>
              <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1">
                {mainColumns.map((column) => (
                  <label
                    key={column.name}
                    className="flex items-center gap-1 text-[11px] text-slate-600"
                  >
                    <input
                      type="checkbox"
                      data-testid="editor-recordview-header-column"
                      data-column={column.name}
                      checked={draft.body.headerFields.some(
                        (f) => f.column === column.name,
                      )}
                      onChange={(e) =>
                        toggleHeaderColumn(column, e.target.checked)
                      }
                    />
                    {column.name}
                  </label>
                ))}
              </div>
              {/* 已勾选但列已漂移（不在列清单）：如实标注，保存自检拦截。 */}
              {draft.body.headerFields
                .filter((f) => !mainColumns.some((c) => c.name === f.column))
                .map((f) => (
                  <label
                    key={f.column}
                    className="mt-1 flex items-center gap-1 text-[11px] text-amber-700"
                  >
                    <input
                      type="checkbox"
                      data-testid="editor-recordview-header-column-missing"
                      data-column={f.column}
                      checked
                      onChange={() =>
                        patchBody({
                          headerFields: draft.body.headerFields.filter(
                            (x) => x.column !== f.column,
                          ),
                        })
                      }
                    />
                    {f.column}
                    <span className="text-[10px]">
                      {t("console.recordview.editor.columnMissing")}
                    </span>
                  </label>
                ))}
            </div>
          )}

          {/* 头部字段卡：仅 label 可改写（缺省=列名）。 */}
          {draft.body.headerFields.length > 0 && (
            <ul data-testid="editor-recordview-field-list" className="mt-2 space-y-1.5">
              {draft.body.headerFields.map((field) => (
                <li
                  key={field.column}
                  data-testid="editor-recordview-field"
                  data-column={field.column}
                  className="flex flex-wrap items-center gap-2 rounded border border-slate-200 p-2"
                >
                  <span className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] text-slate-500">
                    {field.column}
                  </span>
                  <label className="text-[11px] text-slate-400">
                    {t("console.recordview.editor.fieldLabelLabel")}
                  </label>
                  <input
                    data-testid="editor-recordview-field-label"
                    value={field.label ?? ""}
                    placeholder={field.column}
                    onChange={(e) => patchHeaderField(field.column, e.target.value)}
                    className="w-36 rounded border border-slate-300 px-2 py-1 text-xs"
                  />
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* 右：sections 增删配（标题/数据集/日期列/上限/filters）。 */}
        <div className="rounded-md border border-slate-200 bg-white p-3">
          <div className="flex items-center gap-2">
            <h3 className="text-xs font-semibold text-slate-600">
              {t("console.recordview.editor.sectionsLabel")}
            </h3>
            <button
              type="button"
              data-testid="editor-recordview-add-section"
              onClick={addSection}
              disabled={draft.body.sections.length >= RECORD_VIEW_LIMITS.sections}
              className="rounded border border-slate-300 px-2 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100 disabled:opacity-50"
            >
              {t("console.recordview.editor.addSection")}
            </button>
            <span className="text-[10px] text-slate-300">
              {t("console.recordview.editor.sectionsHint")}
            </span>
          </div>
          {draft.body.sections.length === 0 && (
            <p
              data-testid="editor-recordview-empty-sections"
              className="mt-2 text-xs text-slate-400"
            >
              {t("console.recordview.editor.emptySections")}
            </p>
          )}
          <ul data-testid="editor-recordview-section-list" className="mt-2 space-y-2">
            {draft.body.sections.map((section, index) => {
              const sectionColumns = columnsOf(section.datasetId) ?? [];
              return (
                <li
                  key={index}
                  data-testid="editor-recordview-section"
                  data-index={index}
                  className="rounded border border-slate-200 p-2.5"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <label className="text-[11px] text-slate-400">
                      {t("console.recordview.editor.sectionTitleLabel")}
                    </label>
                    <input
                      data-testid="editor-recordview-section-title"
                      value={section.title}
                      onChange={(e) => patchSection(index, { title: e.target.value })}
                      className="w-28 rounded border border-slate-300 px-2 py-1 text-xs"
                    />
                    <label className="text-[11px] text-slate-400">
                      {t("console.recordview.editor.sectionDatasetLabel")}
                    </label>
                    <select
                      data-testid="editor-recordview-section-dataset"
                      value={section.datasetId}
                      onChange={(e) =>
                        patchSection(index, {
                          datasetId: e.target.value,
                          // 换数据集即清日期列（旧列对新集无意义）。
                          dateColumn: "",
                        })
                      }
                      className="rounded border border-slate-300 px-1.5 py-1 text-xs"
                    >
                      <option value="">
                        {t("console.recordview.editor.datasetPlaceholder")}
                      </option>
                      {(datasets ?? []).map((dataset) => (
                        <option key={dataset.id} value={dataset.id}>
                          {datasetOptionText(t, dataset)}
                        </option>
                      ))}
                      {section.datasetId && sectionColumns.length === 0 && (
                        <option value={section.datasetId}>
                          {section.datasetId}
                        </option>
                      )}
                    </select>
                    <label className="text-[11px] text-slate-400">
                      {t("console.recordview.editor.sectionDateColumnLabel")}
                    </label>
                    <select
                      data-testid="editor-recordview-section-date-column"
                      value={section.dateColumn}
                      onChange={(e) =>
                        patchSection(index, { dateColumn: e.target.value })
                      }
                      className="rounded border border-slate-300 px-1.5 py-1 text-xs"
                    >
                      <option value="">
                        {t("console.recordview.editor.dateColumnPlaceholder")}
                      </option>
                      {sectionColumns.map((column) => (
                        <option key={column.name} value={column.name}>
                          {column.name}
                        </option>
                      ))}
                    </select>
                    <label className="text-[11px] text-slate-400">
                      {t("console.recordview.editor.sectionLimitLabel")}
                    </label>
                    <input
                      data-testid="editor-recordview-section-limit"
                      type="number"
                      min={1}
                      max={RECORD_VIEW_LIMITS.limitMax}
                      value={section.limit ?? ""}
                      onChange={(e) =>
                        patchSection(index, {
                          limit: e.target.value === "" ? undefined : Number(e.target.value),
                        })
                      }
                      className="w-16 rounded border border-slate-300 px-2 py-1 text-xs"
                    />
                    <button
                      type="button"
                      data-testid="editor-recordview-remove-section"
                      onClick={() => removeSection(index)}
                      className="ml-auto rounded border border-slate-300 px-2 py-0.5 text-[10px] text-slate-500 hover:bg-slate-100"
                    >
                      {t("console.recordview.editor.removeSection")}
                    </button>
                  </div>

                  {/* filters：值=字面量或 {fromField} 引用主记录字段。 */}
                  <div className="mt-2">
                    <div className="flex items-center gap-2">
                      <span className="text-[11px] text-slate-400">
                        {t("console.recordview.editor.filtersLabel")}
                      </span>
                      <button
                        type="button"
                        data-testid="editor-recordview-add-filter"
                        onClick={() => addFilter(index)}
                        disabled={section.filters.length >= RECORD_VIEW_LIMITS.filters}
                        className="rounded border border-slate-300 px-2 py-0.5 text-[10px] text-slate-600 hover:bg-slate-100 disabled:opacity-50"
                      >
                        {t("console.recordview.editor.addFilter")}
                      </button>
                    </div>
                    <ul className="mt-1 space-y-1">
                      {section.filters.map((filter, filterIndex) => {
                        // 引用形态的当前列名（闭包内窄化保持——照 FormEditor 范式）。
                        const refName = isFromFieldValue(filter.value)
                          ? filter.value.fromField
                          : "";
                        return (
                        <li
                          key={filterIndex}
                          data-testid="editor-recordview-filter-row"
                          data-index={filterIndex}
                          className="flex flex-wrap items-center gap-2"
                        >
                          <select
                            data-testid="editor-recordview-filter-column"
                            value={filter.column}
                            onChange={(e) =>
                              patchFilter(index, filterIndex, {
                                column: e.target.value,
                              })
                            }
                            className="rounded border border-slate-300 px-1.5 py-1 text-xs"
                          >
                            <option value="">
                              {t("console.recordview.editor.filterColumnPlaceholder")}
                            </option>
                            {sectionColumns.map((column) => (
                              <option key={column.name} value={column.name}>
                                {column.name}
                              </option>
                            ))}
                            {filter.column &&
                              !sectionColumns.some(
                                (c) => c.name === filter.column,
                              ) && (
                                <option value={filter.column}>
                                  {filter.column}
                                </option>
                              )}
                          </select>
                          <select
                            data-testid="editor-recordview-filter-op"
                            value={filter.op}
                            onChange={(e) =>
                              patchFilter(index, filterIndex, {
                                op: e.target.value as RecordViewFilterOp,
                                value: parseFilterLiteral(
                                  e.target.value as RecordViewFilterOp,
                                  filterValueText(filter.value),
                                ),
                              })
                            }
                            className="rounded border border-slate-300 px-1.5 py-1 text-xs"
                          >
                            {RECORD_VIEW_FILTER_OPS.map((op) => (
                              <option key={op} value={op}>
                                {t(`console.recordview.editor.filterOps.${op}`)}
                              </option>
                            ))}
                          </select>
                          {isFromFieldValue(filter.value) ? (
                            <>
                              <select
                                data-testid="editor-recordview-filter-value-field"
                                value={filter.value.fromField}
                                onChange={(e) =>
                                  patchFilter(index, filterIndex, {
                                    value: { fromField: e.target.value },
                                  })
                                }
                                className="rounded border border-slate-300 px-1.5 py-1 text-xs"
                              >
                                <option value="">
                                  {t(
                                    "console.recordview.editor.fromFieldPlaceholder",
                                  )}
                                </option>
                                {mainColumns.map((column) => (
                                  <option key={column.name} value={column.name}>
                                    {column.name}
                                  </option>
                                ))}
                                {refName &&
                                  !mainColumns.some((c) => c.name === refName) && (
                                    <option value={refName}>{refName}</option>
                                  )}
                              </select>
                              <button
                                type="button"
                                data-testid="editor-recordview-filter-value-to-literal"
                                onClick={() =>
                                  patchFilter(index, filterIndex, { value: "" })
                                }
                                className="rounded border border-slate-300 px-2 py-0.5 text-[10px] text-slate-500 hover:bg-slate-100"
                              >
                                {t("console.recordview.editor.valueToLiteral")}
                              </button>
                            </>
                          ) : (
                            <>
                              <input
                                data-testid="editor-recordview-filter-value"
                                value={filterValueText(filter.value)}
                                placeholder={t(
                                  "console.recordview.editor.filterValuePlaceholder",
                                )}
                                onChange={(e) =>
                                  patchFilter(index, filterIndex, {
                                    value: parseFilterLiteral(filter.op, e.target.value),
                                  })
                                }
                                className="w-32 rounded border border-slate-300 px-2 py-1 text-xs"
                              />
                              <button
                                type="button"
                                data-testid="editor-recordview-filter-value-to-field"
                                onClick={() =>
                                  patchFilter(index, filterIndex, {
                                    value: {
                                      fromField: mainColumns[0]?.name ?? "",
                                    },
                                  })
                                }
                                className="rounded border border-slate-300 px-2 py-0.5 text-[10px] text-slate-500 hover:bg-slate-100"
                              >
                                {t("console.recordview.editor.valueToFromField")}
                              </button>
                            </>
                          )}
                          <button
                            type="button"
                            data-testid="editor-recordview-remove-filter"
                            onClick={() => removeFilter(index, filterIndex)}
                            className="rounded border border-slate-300 px-2 py-0.5 text-[10px] text-slate-500 hover:bg-slate-100"
                          >
                            {t("console.recordview.editor.removeFilter")}
                          </button>
                        </li>
                        );
                      })}
                    </ul>
                  </div>
                </li>
              );
            })}
          </ul>
        </div>
      </div>

      {/* 试预览（任务 4.2）：键值选择器（去重下拉 ≤500 + 手输兜底）→ 与公开
          扫码页同构的时间轴（console 身份取数，不生成链接）。 */}
      <div
        data-testid="record-view-preview"
        className="mt-3 rounded-md border border-slate-200 bg-slate-50/60 p-3"
      >
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-xs font-semibold text-slate-600">
            {t("console.recordview.preview.title")}
          </h3>
          {!previewReady && (
            <span className="text-[11px] text-slate-400">
              {t("console.recordview.preview.unboundHint")}
            </span>
          )}
        </div>
        {previewReady && (
          <>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <label htmlFor="record-view-preview-key" className="text-[11px] text-slate-400">
                {t("console.recordview.preview.keyLabel")}
              </label>
              <select
                id="record-view-preview-key"
                data-testid="record-view-preview-key-select"
                value={selectedKey}
                onChange={(e) => setSelectedKey(e.target.value)}
                className="rounded border border-slate-300 px-2 py-1 text-xs"
              >
                <option value="">
                  {t("console.recordview.preview.keyPlaceholder")}
                </option>
                {(keyOptions ?? []).map((key) => (
                  <option key={key} value={key}>
                    {key}
                  </option>
                ))}
              </select>
              <label className="text-[11px] text-slate-400">
                {t("console.recordview.preview.manualKeyLabel")}
              </label>
              <input
                data-testid="record-view-preview-key-manual"
                value={manualKey}
                onChange={(e) => setManualKey(e.target.value)}
                placeholder={t("console.recordview.preview.keyPlaceholder")}
                className="w-40 rounded border border-slate-300 px-2 py-1 text-xs"
              />
              <button
                type="button"
                data-testid="record-view-preview-button"
                disabled={!effectiveKey}
                onClick={runPreview}
                className="rounded bg-slate-900 px-3 py-1 text-xs text-white hover:bg-slate-700 disabled:opacity-50"
              >
                {t("console.recordview.preview.button")}
              </button>
              {keyTruncated && (
                <span
                  data-testid="record-view-preview-keys-truncated"
                  className="text-[10px] text-amber-600"
                >
                  {t("console.recordview.preview.keysTruncated", {
                    count: RECORD_VIEW_LIMITS.distinctKeys,
                  })}
                </span>
              )}
            </div>
            {keyOptions == null && !keyLoadError && (
              <p className="mt-1 text-[11px] text-slate-400">
                {t("console.recordview.preview.keysLoading")}
              </p>
            )}
            {keyLoadError && (
              <p className="mt-1 text-[11px] text-red-600">{keyLoadError}</p>
            )}
            {previewKey != null && (
              <div className="mt-3">
                <RecordViewRuntime
                  endpoint={{ kind: "preview", artifactId, recordKey: previewKey }}
                />
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
