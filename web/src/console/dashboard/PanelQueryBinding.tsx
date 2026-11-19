// 面板「绑定查询」表单段（wire-panel-query-binding 任务 2.1）。
//
// DashboardEditor 面板详情区的契约绑定表单：未绑定 → 「未绑定」+「绑定」
// 入口；打开后数据集下拉（名称+tier 徽标+行数）→ 列勾选（=columns 投影，
// 按数据集列序）、过滤行（列+算子+值，可增删）、排序（列+asc/desc，可增删）、
// limit（1..1000）。「试取数据」走 queryPreview（与服务端契约刷新同一执行
// 路径）——成功把 snapshot 写进**本地编辑态面板**（预览即所得，不算保存），
// 并把服务端归一后的 query 写回面板；失败（422/404/400）如实呈现 detail。
// 「解绑」清除面板 query（本地，快照保留）。表单每次变更即把 buildPanelQuery
// 结果写进面板 query（保存走既有 PUT 链，query 随面板落盘）。
// 纯转换函数（buildPanelQuery / formFromPanelQuery）在 queryBinding.ts。
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { apiErrorText, queryPreview, type Dataset } from "../api";
import { normalizePanelQuery, type DashboardPanel } from "./spec";
import {
  QUERY_BINDING_FILTER_OPS,
  buildPanelQuery,
  emptyQueryBindingForm,
  formFromPanelQuery,
  type QueryBindingFormState,
} from "./queryBinding";

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
    ? t("console.dashboard.editor.queryForm.datasetOption", {
        name: dataset.name,
        tier: tierText,
        rows: dataset.row_count,
      })
    : t("console.dashboard.editor.queryForm.datasetOptionPlain", {
        name: dataset.name,
        rows: dataset.row_count,
      });
}

export function PanelQueryBinding({
  panel,
  datasets,
  datasetsLoading,
  datasetsError,
  onEnsureDatasets,
  onPatchPanel,
}: {
  panel: DashboardPanel;
  /** 本企业数据集列表（null=未加载；编辑器层懒加载并缓存）。 */
  datasets: Dataset[] | null;
  datasetsLoading: boolean;
  datasetsError: string | null;
  /** 幂等触发数据集列表加载（表单段打开时调用一次）。 */
  onEnsureDatasets: () => void;
  /** 编辑器 patchPanel：表单 → 面板 query / 试取 → 面板 snapshot 写回。 */
  onPatchPanel: (
    id: string,
    patch: (p: DashboardPanel) => DashboardPanel,
  ) => void;
}) {
  const { t } = useTranslation();
  // 表单打开态：已绑定面板初始即开（回填原值）；未绑定从「绑定」入口打开。
  const [open, setOpen] = useState(() => panel.query != null);
  const [form, setForm] = useState<QueryBindingFormState>(() =>
    formFromPanelQuery(panel.query),
  );
  const [trying, setTrying] = useState(false);
  const [tryError, setTryError] = useState<string | null>(null);
  const [tryOk, setTryOk] = useState<string | null>(null);

  const selectedDataset =
    datasets?.find((d) => d.id === form.datasetId) ?? null;
  const datasetColumns =
    selectedDataset?.columns.map((column) => column.name) ?? undefined;

  useEffect(() => {
    if (open) onEnsureDatasets();
  }, [open, onEnsureDatasets]);

  /** 表单变更：本地态 + 即时把 buildPanelQuery 结果写进面板 query
   *  （未选数据集/全部清空 → 移除 query 字段）。 */
  const applyForm = (next: QueryBindingFormState) => {
    setForm(next);
    onPatchPanel(panel.id, (p) => {
      const query = buildPanelQuery(next, datasetColumns);
      if (query === undefined) {
        const { query: _removed, ...rest } = p;
        return rest;
      }
      return { ...p, query };
    });
  };

  const unbind = () => {
    setForm(emptyQueryBindingForm());
    setTryError(null);
    setTryOk(null);
    setOpen(false);
    onPatchPanel(panel.id, (p) => {
      const { query: _removed, ...rest } = p;
      return rest;
    });
  };

  /** 试取数据：入参=当前表单构建的契约 query；成功写 snapshot（+归一 query）
   *  进本地编辑态面板——预览即所得，不算保存；失败如实呈现后端 detail。 */
  const tryPreview = async () => {
    const query = buildPanelQuery(form, datasetColumns);
    setTryOk(null);
    if (!query) {
      setTryError(t("console.dashboard.editor.queryForm.needDataset"));
      return;
    }
    setTrying(true);
    setTryError(null);
    try {
      const result = await queryPreview(query);
      const normalized = normalizePanelQuery(result.query);
      onPatchPanel(panel.id, (p) => ({
        ...p,
        snapshot: {
          columns: result.snapshot.columns,
          rows: result.snapshot.rows,
        },
        ...(normalized !== undefined ? { query: normalized } : {}),
      }));
      setTryOk(
        t("console.dashboard.editor.queryForm.previewOk", {
          rows: result.snapshot.rows.length,
        }),
      );
    } catch (err) {
      setTryError(
        apiErrorText(err, t("console.dashboard.editor.queryForm.tryFailed")),
      );
    } finally {
      setTrying(false);
    }
  };

  if (!open) {
    return (
      <div
        data-testid="editor-query-section"
        className="mt-2 flex flex-wrap items-center gap-2"
      >
        <label className="text-[11px] text-slate-400">
          {t("console.dashboard.editor.queryForm.sectionLabel")}
        </label>
        <span
          data-testid="editor-query-unbound"
          className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-400"
        >
          {t("console.dashboard.editor.queryForm.unbound")}
        </span>
        <button
          type="button"
          data-testid="editor-query-bind"
          onClick={() => {
            setTryError(null);
            setTryOk(null);
            setOpen(true);
          }}
          className="rounded border border-slate-300 bg-white px-2 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100"
        >
          {t("console.dashboard.editor.queryForm.bind")}
        </button>
      </div>
    );
  }

  /** 勾选列渲染序：数据集列序（=刷新后快照列序）；数据集不在列表（已删/未
   *  加载出列）时退回已知投影列，保证回填勾选态可见。 */
  const columnOptions = datasetColumns ?? form.columns;
  const firstColumn = columnOptions[0] ?? "";

  return (
    <div
      data-testid="editor-query-form"
      className="mt-2 rounded border border-sky-100 bg-sky-50/40 p-2"
    >
      <div className="flex flex-wrap items-center gap-2">
        <label className="text-[11px] font-medium text-slate-500">
          {t("console.dashboard.editor.queryForm.sectionLabel")}
        </label>
        {panel.query && (
          <span
            data-testid="editor-panel-query"
            title={t("console.dashboard.editor.queryForm.boundHint")}
            className="rounded bg-sky-100 px-1.5 py-0.5 text-[10px] text-sky-700"
          >
            {t("console.dashboard.editor.queryBadge")}
            {t("console.dashboard.editor.queryDataset", {
              id: panel.query.dataset_id,
            })}
          </span>
        )}
        <div className="ml-auto">
          <button
            type="button"
            data-testid="editor-query-unbind"
            onClick={unbind}
            className="rounded border border-rose-200 bg-white px-2 py-0.5 text-[11px] text-rose-600 hover:bg-rose-50"
          >
            {t("console.dashboard.editor.queryForm.unbind")}
          </button>
        </div>
      </div>

      <div className="mt-1.5 flex flex-wrap items-center gap-2">
        <label
          htmlFor={`editor-query-dataset-${panel.id}`}
          className="text-[11px] text-slate-400"
        >
          {t("console.dashboard.editor.queryForm.datasetLabel")}
        </label>
        <select
          id={`editor-query-dataset-${panel.id}`}
          data-testid="editor-query-dataset"
          value={form.datasetId}
          onChange={(e) => {
            // 换数据集：投影勾选与过滤/排序列指向旧列，一并清空重来。
            const datasetId = e.target.value;
            applyForm({
              ...emptyQueryBindingForm(),
              datasetId,
              limit: form.limit,
            });
          }}
          className="rounded border border-slate-300 px-1.5 py-0.5 text-xs"
        >
          <option value="">
            {t("console.dashboard.editor.queryForm.datasetPlaceholder")}
          </option>
          {(datasets ?? []).map((dataset) => (
            <option key={dataset.id} value={dataset.id}>
              {datasetOptionText(t, dataset)}
            </option>
          ))}
          {/* 已绑数据集不在列表（已删/他企）——保留占位选项，值不漂移。 */}
          {form.datasetId && !selectedDataset && (
            <option value={form.datasetId}>
              {t("console.dashboard.editor.queryForm.missingDatasetOption", {
                id: form.datasetId,
              })}
            </option>
          )}
        </select>
        {datasetsLoading && (
          <span className="text-[10px] text-slate-400">
            {t("console.dashboard.editor.queryForm.loadingDatasets")}
          </span>
        )}
      </div>
      {datasetsError && (
        <p
          data-testid="editor-query-datasets-error"
          className="mt-1 text-[10px] text-red-600"
        >
          {datasetsError}
        </p>
      )}
      {panel.type === "kpi" && (
        <p
          data-testid="editor-query-kpi-hint"
          className="mt-1 text-[10px] text-slate-400"
        >
          {t("console.dashboard.editor.queryForm.kpiHint")}
        </p>
      )}

      {form.datasetId && (
        <>
          <div className="mt-1.5">
            <span className="text-[11px] text-slate-400">
              {t("console.dashboard.editor.queryForm.columnsLabel")}
            </span>
            <span className="ml-1 text-[10px] text-slate-300">
              {t("console.dashboard.editor.queryForm.columnsHint")}
            </span>
            <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1">
              {columnOptions.map((name) => (
                <label
                  key={name}
                  className="flex items-center gap-1 text-[11px] text-slate-600"
                >
                  <input
                    type="checkbox"
                    data-testid="editor-query-column"
                    data-column={name}
                    checked={form.columns.includes(name)}
                    onChange={(e) => {
                      const checked = e.target.checked;
                      applyForm({
                        ...form,
                        columns: checked
                          ? [...form.columns, name]
                          : form.columns.filter((c) => c !== name),
                      });
                    }}
                  />
                  {name}
                </label>
              ))}
            </div>
          </div>

          <div className="mt-1.5">
            <span className="text-[11px] text-slate-400">
              {t("console.dashboard.editor.queryForm.filtersLabel")}
            </span>
            <div className="mt-1 space-y-1">
              {form.filters.map((row, index) => (
                <div
                  key={index}
                  data-testid="editor-query-filter"
                  className="flex flex-wrap items-center gap-1"
                >
                  <select
                    data-testid="editor-query-filter-column"
                    value={row.column}
                    onChange={(e) => {
                      const filters = [...form.filters];
                      filters[index] = { ...row, column: e.target.value };
                      applyForm({ ...form, filters });
                    }}
                    className="rounded border border-slate-300 px-1 py-0.5 text-[11px]"
                  >
                    {!columnOptions.includes(row.column) && (
                      <option value={row.column}>{row.column}</option>
                    )}
                    {columnOptions.map((name) => (
                      <option key={name} value={name}>
                        {name}
                      </option>
                    ))}
                  </select>
                  <select
                    data-testid="editor-query-filter-op"
                    value={row.op}
                    onChange={(e) => {
                      const filters = [...form.filters];
                      filters[index] = {
                        ...row,
                        op: e.target.value as typeof row.op,
                      };
                      applyForm({ ...form, filters });
                    }}
                    className="rounded border border-slate-300 px-1 py-0.5 text-[11px]"
                  >
                    {QUERY_BINDING_FILTER_OPS.map((op) => (
                      <option key={op} value={op}>
                        {t(`console.dashboard.editor.queryForm.op.${op}`)}
                      </option>
                    ))}
                  </select>
                  <input
                    data-testid="editor-query-filter-value"
                    value={row.value}
                    onChange={(e) => {
                      const filters = [...form.filters];
                      filters[index] = { ...row, value: e.target.value };
                      applyForm({ ...form, filters });
                    }}
                    className="w-28 rounded border border-slate-300 px-1.5 py-0.5 text-[11px]"
                  />
                  <button
                    type="button"
                    data-testid="editor-query-filter-remove"
                    onClick={() =>
                      applyForm({
                        ...form,
                        filters: form.filters.filter((_, i) => i !== index),
                      })
                    }
                    className="rounded border border-slate-300 bg-white px-1.5 py-0.5 text-[11px] text-slate-500 hover:bg-slate-100"
                  >
                    {t("console.dashboard.editor.queryForm.removeRow")}
                  </button>
                </div>
              ))}
              <button
                type="button"
                data-testid="editor-query-filter-add"
                onClick={() =>
                  applyForm({
                    ...form,
                    filters: [
                      ...form.filters,
                      { column: firstColumn, op: "eq", value: "" },
                    ],
                  })
                }
                className="rounded border border-slate-300 bg-white px-2 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100"
              >
                {t("console.dashboard.editor.queryForm.addFilter")}
              </button>
            </div>
          </div>

          <div className="mt-1.5">
            <span className="text-[11px] text-slate-400">
              {t("console.dashboard.editor.queryForm.sortLabel")}
            </span>
            <div className="mt-1 space-y-1">
              {form.sort.map((row, index) => (
                <div
                  key={index}
                  data-testid="editor-query-sort"
                  className="flex flex-wrap items-center gap-1"
                >
                  <select
                    data-testid="editor-query-sort-column"
                    value={row.column}
                    onChange={(e) => {
                      const sort = [...form.sort];
                      sort[index] = { ...row, column: e.target.value };
                      applyForm({ ...form, sort });
                    }}
                    className="rounded border border-slate-300 px-1 py-0.5 text-[11px]"
                  >
                    {!columnOptions.includes(row.column) && (
                      <option value={row.column}>{row.column}</option>
                    )}
                    {columnOptions.map((name) => (
                      <option key={name} value={name}>
                        {name}
                      </option>
                    ))}
                  </select>
                  <select
                    data-testid="editor-query-sort-dir"
                    value={row.dir}
                    onChange={(e) => {
                      const sort = [...form.sort];
                      sort[index] = {
                        ...row,
                        dir: e.target.value as "asc" | "desc",
                      };
                      applyForm({ ...form, sort });
                    }}
                    className="rounded border border-slate-300 px-1 py-0.5 text-[11px]"
                  >
                    <option value="asc">
                      {t("console.dashboard.editor.queryForm.dirAsc")}
                    </option>
                    <option value="desc">
                      {t("console.dashboard.editor.queryForm.dirDesc")}
                    </option>
                  </select>
                  <button
                    type="button"
                    data-testid="editor-query-sort-remove"
                    onClick={() =>
                      applyForm({
                        ...form,
                        sort: form.sort.filter((_, i) => i !== index),
                      })
                    }
                    className="rounded border border-slate-300 bg-white px-1.5 py-0.5 text-[11px] text-slate-500 hover:bg-slate-100"
                  >
                    {t("console.dashboard.editor.queryForm.removeRow")}
                  </button>
                </div>
              ))}
              <button
                type="button"
                data-testid="editor-query-sort-add"
                onClick={() =>
                  applyForm({
                    ...form,
                    sort: [...form.sort, { column: firstColumn, dir: "asc" }],
                  })
                }
                className="rounded border border-slate-300 bg-white px-2 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100"
              >
                {t("console.dashboard.editor.queryForm.addSort")}
              </button>
            </div>
          </div>

          <div className="mt-1.5 flex flex-wrap items-center gap-2">
            <label
              htmlFor={`editor-query-limit-${panel.id}`}
              className="text-[11px] text-slate-400"
            >
              {t("console.dashboard.editor.queryForm.limitLabel")}
            </label>
            <input
              id={`editor-query-limit-${panel.id}`}
              data-testid="editor-query-limit"
              type="number"
              min={1}
              max={1000}
              step={1}
              value={form.limit}
              onChange={(e) => applyForm({ ...form, limit: e.target.value })}
              className="w-20 rounded border border-slate-300 px-1.5 py-0.5 text-[11px] tabular-nums"
            />
          </div>
        </>
      )}

      <div className="mt-1.5 flex flex-wrap items-center gap-2">
        <button
          type="button"
          data-testid="editor-query-try"
          disabled={trying}
          onClick={() => void tryPreview()}
          className="rounded bg-slate-900 px-2.5 py-0.5 text-[11px] text-white hover:bg-slate-700 disabled:opacity-50"
        >
          {trying
            ? t("console.dashboard.editor.queryForm.trying")
            : t("console.dashboard.editor.queryForm.tryButton")}
        </button>
        {tryOk && (
          <span
            data-testid="editor-query-try-ok"
            className="text-[10px] text-emerald-600"
          >
            {tryOk}
          </span>
        )}
        {tryError && (
          <span
            data-testid="editor-query-error"
            className="text-[10px] text-red-600"
          >
            {tryError}
          </span>
        )}
      </div>
    </div>
  );
}
