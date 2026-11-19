// 仪表盘编辑器（wire-dashboard-renderer 任务 3 / wire-dashboard-editor-v2）。
// v1：面板增（选 type 出模板）/删/改标题/移序/span 三档切换；kpi 值（存
//   snapshot.rows[0][0]）/单位/副标题表单编辑。
// v2：图表类型互转（convertPanelType——line↔bar↔table 保 snapshot 零损失；
//   kpi↔图表互转先 confirm，信息取舍见 spec.ts）；面板复制（duplicatePanel
//   新 id+副本标题+深拷贝）；source 文本编辑（刷新依赖此标注）；config 表单
//   按 type 展开（kpi=单位/副标题，line/bar=x/y 轴名）；未保存变更计数
//   （draft 与已保存基线按面板深比较的差异数）。
// wire-dashboard-query-contract 任务 2.2：带 query 的面板在详情区显示
//   「已绑定查询」徽标 + dataset_id；增删/移序/复制/互转/保存一律保留 query
//   不抹掉（helpers 结构化拷贝天然覆盖，spec.test 断言）。
// wire-panel-query-binding 任务 2.1：徽标升级为「绑定查询」表单段
//   （PanelQueryBinding）——数据集选择/列勾选/过滤/排序/limit + 试取数据
//   （queryPreview → snapshot 写进本地编辑态面板）+ 解绑 + 已有 query 回填；
//   数据集列表编辑器层懒加载一次共享。
// 编辑态实时预览复用 DashboardRenderer；保存走既有 PUT（saveArtifactSpec，
//   version 递增由服务端）；未保存离开 confirm 提示（含 beforeunload 兜底）。
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  apiErrorText,
  fetchDatasets,
  saveArtifactSpec,
  type ArtifactDetail,
  type Dataset,
} from "../api";
import {
  DASHBOARD_ADVANCED_PANEL_TYPES,
  DASHBOARD_BASE_PANEL_TYPES,
  DASHBOARD_PANEL_SPANS,
  DASHBOARD_PANEL_TYPES,
  buildArtifactEnvelope,
  convertPanelType,
  duplicatePanel,
  moveItem,
  panelTemplate,
  type DashboardPanel,
  type DashboardPanelConfig,
  type DashboardPanelType,
  type DashboardSpecV1,
} from "./spec";
import { DashboardRenderer } from "./DashboardRenderer";
import { PanelQueryBinding } from "./PanelQueryBinding";
import {
  PanelEncodingForm,
  PanelHeightForm,
  PanelOverlaysForm,
  RawOptionViewer,
  ListEncodingForm,
  showsListEncodingForm,
  showsEncodingForm,
  showsOverlaysForm,
} from "./PanelV2Forms";

/** 表单输入 → 契约格子值：空 → null；完整数字样式 → number；其余（含
 *  输入中的 "3." 等未完成态）原样字符串——不打断受控输入。 */
function coerceCell(text: string): string | number | null {
  if (text === "") return null;
  const n = Number(text);
  if (Number.isFinite(n) && !text.endsWith(".")) return n;
  return text;
}

export function DashboardEditor({
  artifactId,
  initial,
  onSaved,
  onExit,
}: {
  artifactId: number;
  /** 服务端当前 spec 解析出的 v1 本体（面板 id 已归一）。 */
  initial: DashboardSpecV1;
  onSaved: (detail: ArtifactDetail) => void;
  /** 返回查看态（脏态时编辑器先 confirm）。 */
  onExit: () => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState<DashboardSpecV1>(initial);
  // 已保存基线（保存成功后推进——dirty 对比用它而不是 initial prop）。
  const [baseline, setBaseline] = useState<DashboardSpecV1>(initial);
  const [addType, setAddType] = useState<DashboardPanelType>("kpi");
  const [saving, setSaving] = useState(false);
  const [savedVersion, setSavedVersion] = useState<number | null>(null);
  const [serverError, setServerError] = useState<string | null>(null);

  // 「绑定查询」表单的数据集列表：编辑器层懒加载一次（首个表单段打开时），
  // 全部面板共享；失败如实呈现（可随再次打开重试）。
  const [datasets, setDatasets] = useState<Dataset[] | null>(null);
  const [datasetsLoading, setDatasetsLoading] = useState(false);
  const [datasetsError, setDatasetsError] = useState<string | null>(null);
  const datasetsStartedRef = useRef(false);
  const ensureDatasets = useCallback(() => {
    if (datasetsStartedRef.current) return;
    datasetsStartedRef.current = true;
    setDatasetsLoading(true);
    setDatasetsError(null);
    fetchDatasets()
      .then((list) => setDatasets(list))
      .catch((err) => {
        // 失败允许重试：复位启动标记，再次打开表单段即重新拉取。
        datasetsStartedRef.current = false;
        setDatasetsError(
          apiErrorText(
            err,
            t("console.dashboard.editor.queryForm.datasetsLoadFailed"),
          ),
        );
      })
      .finally(() => setDatasetsLoading(false));
  }, [t]);

  const dirty = useMemo(
    () => JSON.stringify(draft) !== JSON.stringify(baseline),
    [draft, baseline],
  );

  /** 未保存变更计数：draft 与已保存基线按面板 id 对齐深比较——改动/新增/删除
   *  各计 1（面板维度；看板标题改动不计入面板数）。 */
  const unsavedPanelCount = useMemo(() => {
    const baselineById = new Map(baseline.panels.map((p) => [p.id, p]));
    let count = 0;
    const seen = new Set<string>();
    for (const panel of draft.panels) {
      seen.add(panel.id);
      const before = baselineById.get(panel.id);
      if (!before || JSON.stringify(before) !== JSON.stringify(panel)) count += 1;
    }
    for (const panel of baseline.panels) {
      if (!seen.has(panel.id)) count += 1;
    }
    return count;
  }, [draft.panels, baseline.panels]);

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

  const patchPanel = (id: string, patch: (p: DashboardPanel) => DashboardPanel) => {
    setDraft((prev) => ({
      ...prev,
      panels: prev.panels.map((p) => (p.id === id ? patch(p) : p)),
    }));
    setSavedVersion(null);
    setServerError(null);
  };

  const addPanel = () => {
    setDraft((prev) => ({
      ...prev,
      panels: [
        ...prev.panels,
        panelTemplate(
          addType,
          prev.panels.map((p) => p.id),
        ),
      ],
    }));
    setSavedVersion(null);
    setServerError(null);
  };

  const removePanel = (id: string) => {
    setDraft((prev) => ({
      ...prev,
      panels: prev.panels.filter((p) => p.id !== id),
    }));
    setSavedVersion(null);
    setServerError(null);
  };

  const movePanel = (index: number, delta: -1 | 1) => {
    setDraft((prev) => ({
      ...prev,
      panels: moveItem(prev.panels, index, index + delta),
    }));
    setSavedVersion(null);
    setServerError(null);
  };

  /** 类型转换（v2）：kpi↔图表、list↔图表先 confirm（needConfirm 由纯函数
   *  标记；list↔table 同源表格语义不确认）。文案按是否涉及 list 区分。 */
  const convertPanel = (id: string, target: DashboardPanelType) => {
    const current = draft.panels.find((p) => p.id === id);
    if (!current || current.type === target) return;
    const result = convertPanelType(current, target);
    if (result.needConfirm) {
      const isChartType = (type: DashboardPanelType) =>
        type !== "kpi" && type !== "table" && type !== "list";
      const listConversion =
        (current.type === "list" && isChartType(target)) ||
        (target === "list" && isChartType(current.type));
      const message = t(
        listConversion
          ? "console.dashboard.editor.convertConfirmList"
          : "console.dashboard.editor.convertConfirm",
      );
      if (!window.confirm(message)) return;
    }
    patchPanel(id, () => result.panel);
  };

  /** 面板复制（v2）：副本插到原面板之后（新 id 避开全部占用）。 */
  const duplicateAt = (id: string) => {
    setDraft((prev) => {
      const index = prev.panels.findIndex((p) => p.id === id);
      if (index < 0) return prev;
      const copy = duplicatePanel(
        prev.panels[index]!,
        prev.panels.map((p) => p.id),
        t("console.dashboard.editor.duplicateSuffix"),
      );
      const panels = [...prev.panels];
      panels.splice(index + 1, 0, copy);
      return { ...prev, panels };
    });
    setSavedVersion(null);
    setServerError(null);
  };

  /** config 局部写回（v2 的 x/y 轴名与后续 config 字段共用）。 */
  const patchPanelConfig = (id: string, patch: Partial<DashboardPanelConfig>) => {
    patchPanel(id, (p) => ({ ...p, config: { ...p.config, ...patch } }));
  };

  /** kpi 三件套写回：值进 snapshot.rows[0][0]，单位/副标题进 config。 */
  const patchKpi = (
    id: string,
    patch: Partial<Pick<NonNullable<DashboardPanel["config"]>, "unit" | "subtitle">> & {
      value?: string;
    },
  ) => {
    patchPanel(id, (p) => {
      if (patch.value !== undefined) {
        return {
          ...p,
          snapshot: {
            columns: p.snapshot?.columns ?? ["value"],
            rows: [[coerceCell(patch.value)]],
          },
        };
      }
      return {
        ...p,
        config: {
          unit: patch.unit !== undefined ? patch.unit : p.config?.unit ?? "",
          subtitle:
            patch.subtitle !== undefined ? patch.subtitle : p.config?.subtitle ?? "",
        },
      };
    });
  };

  const save = async () => {
    const title = draft.title.trim();
    if (!title || saving) return;
    setSaving(true);
    setServerError(null);
    try {
      const detail = await saveArtifactSpec(
        artifactId,
        buildArtifactEnvelope({ ...draft, title }),
      );
      setSavedVersion(detail.version);
      // 推进基线为已保存内容（服务端按原样存储，回读无漂移）。
      const saved: DashboardSpecV1 = { title, panels: draft.panels };
      setBaseline(JSON.parse(JSON.stringify(saved)) as DashboardSpecV1);
      onSaved(detail);
    } catch (err) {
      setServerError(
        apiErrorText(err, t("console.dashboard.editor.saveFailed")),
      );
    } finally {
      setSaving(false);
    }
  };

  const requestExit = () => {
    if (dirty && !window.confirm(t("console.dashboard.editor.dirtyLeaveConfirm"))) {
      return;
    }
    onExit();
  };

  const previewSpec = useMemo<DashboardSpecV1>(
    () => ({ title: draft.title, panels: draft.panels }),
    [draft],
  );

  return (
    <div data-testid="dashboard-editor" className="mt-2">
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor="dashboard-editor-title" className="text-xs text-slate-500">
          {t("console.dashboard.editor.titleLabel")}
        </label>
        <input
          id="dashboard-editor-title"
          data-testid="editor-dashboard-title"
          value={draft.title}
          onChange={(e) => {
            setDraft((prev) => ({ ...prev, title: e.target.value }));
            setSavedVersion(null);
            setServerError(null);
          }}
          className="w-56 rounded-md border border-slate-300 px-2 py-1 text-sm"
        />
        {unsavedPanelCount > 0 && (
          <span
            data-testid="editor-unsaved-count"
            title={t("console.dashboard.editor.unsavedTitle")}
            className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] text-amber-700"
          >
            {t("console.dashboard.editor.unsavedCount", { n: unsavedPanelCount })}
          </span>
        )}
        <div className="ml-auto flex gap-2">
          <button
            type="button"
            data-testid="editor-exit"
            onClick={requestExit}
            className="rounded border border-slate-300 bg-white px-3 py-1 text-xs text-slate-600 hover:bg-slate-100"
          >
            {t("console.dashboard.editor.backToView")}
          </button>
          <button
            type="button"
            data-testid="editor-save"
            disabled={saving || !draft.title.trim()}
            onClick={() => void save()}
            className="rounded bg-slate-900 px-3 py-1 text-xs text-white hover:bg-slate-700 disabled:opacity-50"
          >
            {saving
              ? t("console.dashboard.editor.saving")
              : t("console.dashboard.editor.save")}
          </button>
        </div>
      </div>
      {savedVersion != null && (
        <p data-testid="editor-saved-version" className="mt-1 text-[11px] text-emerald-600">
          {t("console.dashboard.editor.savedVersion", { version: savedVersion })}
        </p>
      )}
      {serverError && (
        <p data-testid="editor-server-error" className="mt-1 text-[11px] text-red-600">
          {serverError}
        </p>
      )}

      <div className="mt-3 grid grid-cols-1 gap-3 lg:grid-cols-2">
        <div className="rounded-md border border-slate-200 bg-white p-3">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-xs font-semibold text-slate-600">
              {t("console.dashboard.editor.panelListTitle")}
            </h3>
            <div className="ml-auto flex items-center gap-1">
              <label htmlFor="editor-add-type" className="text-[11px] text-slate-400">
                {t("console.dashboard.editor.addTypeLabel")}
              </label>
              <select
                id="editor-add-type"
                data-testid="editor-add-type"
                value={addType}
                onChange={(e) => setAddType(e.target.value as DashboardPanelType)}
                className="rounded border border-slate-300 px-1.5 py-0.5 text-xs"
              >
                <optgroup label={t("console.dashboard.editor.typeGroupBase")}>
                  {DASHBOARD_BASE_PANEL_TYPES.map((type) => (
                    <option key={type} value={type}>
                      {t(`console.dashboard.type.${type}`)}
                    </option>
                  ))}
                </optgroup>
                <optgroup label={t("console.dashboard.editor.typeGroupAdvanced")}>
                  {DASHBOARD_ADVANCED_PANEL_TYPES.map((type) => (
                    <option key={type} value={type}>
                      {t(`console.dashboard.type.${type}`)}
                    </option>
                  ))}
                </optgroup>              </select>
              <button
                type="button"
                data-testid="editor-add-panel"
                onClick={addPanel}
                className="rounded border border-slate-300 bg-white px-2 py-0.5 text-xs text-slate-600 hover:bg-slate-100"
              >
                {t("console.dashboard.editor.addPanel")}
              </button>
            </div>
          </div>

          {draft.panels.length === 0 && (
            <p data-testid="editor-empty-panels" className="mt-2 text-xs text-slate-400">
              {t("console.dashboard.emptyPanels")}
            </p>
          )}

          <ul data-testid="editor-panel-list" className="mt-2 space-y-2">
            {draft.panels.map((panel, index) => (
              <li
                key={panel.id}
                data-testid="editor-panel"
                data-panel-id={panel.id}
                data-panel-type={panel.type}
                className="rounded border border-slate-200 p-2.5"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-500">
                    {t(`console.dashboard.type.${panel.type}`)}
                  </span>
                  <input
                    data-testid="editor-panel-title"
                    value={panel.title}
                    placeholder={t("console.dashboard.editor.panelTitlePlaceholder")}
                    onChange={(e) =>
                      patchPanel(panel.id, (p) => ({ ...p, title: e.target.value }))
                    }
                    className="min-w-32 flex-1 rounded border border-slate-300 px-2 py-1 text-xs"
                  />
                  <div className="flex items-center gap-0.5">
                    {DASHBOARD_PANEL_SPANS.map((span) => (
                      <button
                        key={span}
                        type="button"
                        data-testid="editor-panel-span"
                        data-span={span}
                        onClick={() =>
                          patchPanel(panel.id, (p) => ({ ...p, span }))
                        }
                        className={`rounded px-1.5 py-0.5 text-[11px] ${
                          panel.span === span
                            ? "bg-slate-900 text-white"
                            : "border border-slate-300 bg-white text-slate-600 hover:bg-slate-100"
                        }`}
                      >
                        {t("console.dashboard.editor.spanOption", { span })}
                      </button>
                    ))}
                  </div>
                  <div className="flex items-center gap-1">
                    <button
                      type="button"
                      data-testid="editor-panel-move-left"
                      disabled={index === 0}
                      onClick={() => movePanel(index, -1)}
                      className="rounded border border-slate-300 bg-white px-1.5 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100 disabled:opacity-40"
                    >
                      {t("console.dashboard.editor.moveUp")}
                    </button>
                    <button
                      type="button"
                      data-testid="editor-panel-move-right"
                      disabled={index === draft.panels.length - 1}
                      onClick={() => movePanel(index, 1)}
                      className="rounded border border-slate-300 bg-white px-1.5 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100 disabled:opacity-40"
                    >
                      {t("console.dashboard.editor.moveDown")}
                    </button>
                    <button
                      type="button"
                      data-testid="editor-panel-duplicate"
                      onClick={() => duplicateAt(panel.id)}
                      className="rounded border border-slate-300 bg-white px-1.5 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100"
                    >
                      {t("console.dashboard.editor.duplicate")}
                    </button>
                    <select
                      aria-label={t("console.dashboard.editor.convertLabel")}
                      data-testid="editor-panel-convert"
                      value=""
                      onChange={(e) => {
                        const target = e.target.value as DashboardPanelType;
                        if (target) convertPanel(panel.id, target);
                      }}
                      className="rounded border border-slate-300 bg-white px-1 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100"
                    >
                      <option value="">
                        {t("console.dashboard.editor.convertLabel")}
                      </option>
                      {DASHBOARD_PANEL_TYPES.filter((type) => type !== panel.type).map(
                        (type) => (
                          <option key={type} value={type}>
                            {t(`console.dashboard.editor.convertTo.${type}`)}
                          </option>
                        ),
                      )}
                    </select>
                    <button
                      type="button"
                      data-testid="editor-panel-remove"
                      onClick={() => removePanel(panel.id)}
                      className="rounded border border-rose-200 bg-white px-1.5 py-0.5 text-[11px] text-rose-600 hover:bg-rose-50"
                    >
                      {t("console.dashboard.editor.remove")}
                    </button>
                  </div>
                </div>
                {/* source 文本（v2）：指标 code / 数据集名——手动刷新按此标注取数。 */}
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <label className="text-[11px] text-slate-400">
                    {t("console.dashboard.editor.sourceLabel")}
                  </label>
                  <input
                    data-testid="editor-panel-source"
                    value={panel.source ?? ""}
                    placeholder={t("console.dashboard.editor.sourcePlaceholder")}
                    onChange={(e) =>
                      patchPanel(panel.id, (p) => ({ ...p, source: e.target.value }))
                    }
                    className="w-56 rounded border border-slate-300 px-2 py-1 font-mono text-xs"
                  />
                </div>
                {/* v2 raw_option 面板（任务 4.5）：只读结构查看；不提供编码
                    表单与查询绑定（其数据自含于配置，契约刷新对它一律 kept）。 */}
                {panel.raw_option !== undefined ? (
                  <RawOptionViewer panel={panel} />
                ) : (
                  <>
                    {showsListEncodingForm(panel) && (
                      <ListEncodingForm panel={panel} onPatch={(fn) => patchPanel(panel.id, fn)} />
                    )}
                    {showsEncodingForm(panel) && (
                      <PanelEncodingForm panel={panel} onPatch={(fn) => patchPanel(panel.id, fn)} />
                    )}
                    {showsOverlaysForm(panel) && (
                      <PanelOverlaysForm panel={panel} onPatch={(fn) => patchPanel(panel.id, fn)} />
                    )}
                    {/* 绑定查询段（wire-panel-query-binding 任务 2.1）：未绑定 →
                        「未绑定」+「绑定」入口；已绑定回填原值展开表单（试取/解绑
                        见 PanelQueryBinding；query 随面板字段走既有保存链）。 */}
                    <PanelQueryBinding
                      key={panel.id}
                      panel={panel}
                      datasets={datasets}
                      datasetsLoading={datasetsLoading}
                      datasetsError={datasetsError}
                      onEnsureDatasets={ensureDatasets}
                      onPatchPanel={patchPanel}
                    />
                  </>
                )}
                <PanelHeightForm panel={panel} onPatch={(fn) => patchPanel(panel.id, fn)} />
                {panel.type === "kpi" && (
                  <div className="mt-2 flex flex-wrap items-center gap-2">
                    <label className="text-[11px] text-slate-400">
                      {t("console.dashboard.editor.kpiValue")}
                    </label>
                    <input
                      data-testid="editor-kpi-value"
                      value={String(panel.snapshot?.rows?.[0]?.[0] ?? "")}
                      onChange={(e) => patchKpi(panel.id, { value: e.target.value })}
                      className="w-28 rounded border border-slate-300 px-2 py-1 text-xs tabular-nums"
                    />
                    <label className="text-[11px] text-slate-400">
                      {t("console.dashboard.editor.kpiUnit")}
                    </label>
                    <input
                      data-testid="editor-kpi-unit"
                      value={panel.config?.unit ?? ""}
                      onChange={(e) => patchKpi(panel.id, { unit: e.target.value })}
                      className="w-20 rounded border border-slate-300 px-2 py-1 text-xs"
                    />
                    <label className="text-[11px] text-slate-400">
                      {t("console.dashboard.editor.kpiSubtitle")}
                    </label>
                    <input
                      data-testid="editor-kpi-subtitle"
                      value={panel.config?.subtitle ?? ""}
                      onChange={(e) => patchKpi(panel.id, { subtitle: e.target.value })}
                      className="w-44 rounded border border-slate-300 px-2 py-1 text-xs"
                    />
                  </div>
                )}
                {(panel.type === "line" || panel.type === "bar") && (
                  <div className="mt-2 flex flex-wrap items-center gap-2">
                    <label className="text-[11px] text-slate-400">
                      {t("console.dashboard.editor.xNameLabel")}
                    </label>
                    <input
                      data-testid="editor-panel-xname"
                      value={panel.config?.xName ?? ""}
                      onChange={(e) =>
                        patchPanelConfig(panel.id, { xName: e.target.value })
                      }
                      className="w-28 rounded border border-slate-300 px-2 py-1 text-xs"
                    />
                    <label className="text-[11px] text-slate-400">
                      {t("console.dashboard.editor.yNameLabel")}
                    </label>
                    <input
                      data-testid="editor-panel-yname"
                      value={panel.config?.yName ?? ""}
                      onChange={(e) =>
                        patchPanelConfig(panel.id, { yName: e.target.value })
                      }
                      className="w-28 rounded border border-slate-300 px-2 py-1 text-xs"
                    />
                  </div>
                )}
              </li>
            ))}
          </ul>
        </div>

        <div className="rounded-md border border-slate-200 bg-white p-3">
          <h3 className="text-xs font-semibold text-slate-600">
            {t("console.dashboard.editor.preview")}
          </h3>
          <div className="mt-2">
            <DashboardRenderer spec={previewSpec} />
          </div>
        </div>
      </div>
    </div>
  );
}
