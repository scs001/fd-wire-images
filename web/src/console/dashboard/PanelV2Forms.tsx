// 面板 v2 表单段（wire-chart-contract-v2 任务 4.2–4.5）：
// 编码通道（x 列 / y 列多选 / 轴类型 / 轴名）、叠加层（阈值线 / 区间 / 事件点）、
// 面板高度，以及 raw_option 面板的只读结构查看。
//
// 设计取舍（design D8）：这里只做"类型化子集"的编辑——不做逐类型的专属表单
// （K 线涨跌配色、桑基节点布局之类），那会把编辑器推成图表 IDE。表达力不足
// 的场景走 raw_option 承载，而 raw_option 面板在本段是**只读结构查看**。
import { useTranslation } from "react-i18next";
import {
  DASHBOARD_AXIS_TYPES,
  type DashboardPanel,
  type DashboardPanelEncoding,
  type DashboardPanelOverlays,
  type DashboardPanelType,
} from "./spec";

/** 支持 x/y 编码（笛卡尔）的类型——无轴类型不显示编码表单。 */
const CARTESIAN_UI_TYPES: ReadonlySet<DashboardPanelType> = new Set([
  "line",
  "bar",
  "scatter",
  "candlestick",
  "heatmap",
  "boxplot",
]);

const fieldClass = "rounded border border-slate-300 px-1.5 py-0.5 text-xs";
const labelClass = "text-[11px] text-slate-400";

/**
 * 编码通道表单：x 列 / y 列（多选）/ 轴类型 / 轴名。
 *
 * y 列用原生多选（multiple size=1 观感差，故用勾选列表）——数据集列可能很多，
 * 但快照列通常十来个以内，勾选比多选下拉更可读。
 */
export function PanelEncodingForm({
  panel,
  onPatch,
}: {
  panel: DashboardPanel;
  onPatch: (
    updater: (panel: DashboardPanel) => DashboardPanel,
  ) => void;
}) {
  const { t } = useTranslation();
  const columns = panel.snapshot?.columns ?? [];
  const encoding: DashboardPanelEncoding = panel.encoding ?? {};

  const patchEncoding = (next: DashboardPanelEncoding) => {
    onPatch((current) => {
      // 归一后为空对象即清除字段（不落 encoding: {}——那会让 isV2Panel 误判）
      const hasAny = Object.values(next).some((value) =>
        Array.isArray(value) ? value.length > 0 : value !== undefined,
      );
      if (!hasAny) {
        const { encoding: _drop, ...rest } = current;
        return rest;
      }
      return { ...current, encoding: next };
    });
  };

  const toggleY = (column: string) => {
    const current = encoding.y ?? [];
    const next = current.includes(column)
      ? current.filter((name) => name !== column)
      : [...current, column];
    patchEncoding({ ...encoding, y: next });
  };

  return (
    <div
      data-testid="editor-panel-encoding"
      className="mt-2 rounded border border-slate-200 bg-slate-50/50 p-2"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[11px] font-medium text-slate-500">
          {t("console.dashboard.editor.encodingTitle")}
        </span>
        {columns.length === 0 && (
          <span className={labelClass}>
            {t("console.dashboard.editor.encodingNeedsData")}
          </span>
        )}
      </div>

      {columns.length > 0 && (
        <>
          <div className="mt-1.5 flex flex-wrap items-center gap-2">
            <label className={labelClass} htmlFor={`enc-x-${panel.id}`}>
              {t("console.dashboard.editor.encodingX")}
            </label>
            <select
              id={`enc-x-${panel.id}`}
              data-testid="editor-encoding-x"
              value={encoding.x ?? ""}
              onChange={(e) =>
                patchEncoding({ ...encoding, x: e.target.value || undefined })
              }
              className={fieldClass}
            >
              <option value="">{t("console.dashboard.editor.encodingXDefault")}</option>
              {columns.map((column) => (
                <option key={column} value={column}>
                  {column}
                </option>
              ))}
            </select>

            <label className={labelClass} htmlFor={`enc-xtype-${panel.id}`}>
              {t("console.dashboard.editor.encodingXType")}
            </label>
            <select
              id={`enc-xtype-${panel.id}`}
              data-testid="editor-encoding-xtype"
              value={encoding.xType ?? "category"}
              onChange={(e) =>
                patchEncoding({
                  ...encoding,
                  xType: e.target.value as DashboardPanelEncoding["xType"],
                })
              }
              className={fieldClass}
            >
              {DASHBOARD_AXIS_TYPES.map((axis) => (
                <option key={axis} value={axis}>
                  {t(`console.dashboard.axisType.${axis}`)}
                </option>
              ))}
            </select>

            <label className={labelClass} htmlFor={`enc-ytype-${panel.id}`}>
              {t("console.dashboard.editor.encodingYType")}
            </label>
            <select
              id={`enc-ytype-${panel.id}`}
              data-testid="editor-encoding-ytype"
              value={encoding.yType ?? "value"}
              onChange={(e) =>
                patchEncoding({
                  ...encoding,
                  yType: e.target.value as DashboardPanelEncoding["yType"],
                })
              }
              className={fieldClass}
            >
              {DASHBOARD_AXIS_TYPES.map((axis) => (
                <option key={axis} value={axis}>
                  {t(`console.dashboard.axisType.${axis}`)}
                </option>
              ))}
            </select>
          </div>

          <div className="mt-1.5">
            <span className={labelClass}>
              {t("console.dashboard.editor.encodingY")}
            </span>
            <div className="mt-1 flex flex-wrap gap-2">
              {columns.map((column) => {
                const checked = (encoding.y ?? []).includes(column);
                return (
                  <label
                    key={column}
                    data-testid="editor-encoding-y-option"
                    className={`flex cursor-pointer items-center gap-1 rounded border px-1.5 py-0.5 text-[11px] ${
                      checked
                        ? "border-slate-900 bg-slate-900 text-white"
                        : "border-slate-300 bg-white text-slate-600"
                    }`}
                  >
                    <input
                      type="checkbox"
                      className="sr-only"
                      checked={checked}
                      onChange={() => toggleY(column)}
                    />
                    {column}
                  </label>
                );
              })}
            </div>
          </div>

          <div className="mt-1.5 flex flex-wrap items-center gap-2">
            <label className={labelClass} htmlFor={`enc-xname-${panel.id}`}>
              {t("console.dashboard.editor.encodingXName")}
            </label>
            <input
              id={`enc-xname-${panel.id}`}
              data-testid="editor-encoding-xname"
              value={encoding.xName ?? ""}
              onChange={(e) =>
                patchEncoding({ ...encoding, xName: e.target.value || undefined })
              }
              className={`${fieldClass} w-28`}
            />
            <label className={labelClass} htmlFor={`enc-yname-${panel.id}`}>
              {t("console.dashboard.editor.encodingYName")}
            </label>
            <input
              id={`enc-yname-${panel.id}`}
              data-testid="editor-encoding-yname"
              value={encoding.yName ?? ""}
              onChange={(e) =>
                patchEncoding({ ...encoding, yName: e.target.value || undefined })
              }
              className={`${fieldClass} w-28`}
            />
          </div>
        </>
      )}
    </div>
  );
}

/** 叠加层表单：阈值线（值 + 标签 + 线型）、区间（起止 + 标签）。 */
export function PanelOverlaysForm({
  panel,
  onPatch,
}: {
  panel: DashboardPanel;
  onPatch: (updater: (panel: DashboardPanel) => DashboardPanel) => void;
}) {
  const { t } = useTranslation();
  const overlays: DashboardPanelOverlays = panel.overlays ?? {};
  const lines = overlays.markLine ?? [];
  const areas = overlays.markArea ?? [];

  const patchOverlays = (next: DashboardPanelOverlays) => {
    onPatch((current) => {
      const empty =
        (next.markLine?.length ?? 0) === 0 &&
        (next.markArea?.length ?? 0) === 0 &&
        (next.markPoint?.length ?? 0) === 0;
      if (empty) {
        const { overlays: _drop, ...rest } = current;
        return rest;
      }
      return { ...current, overlays: next };
    });
  };

  /** 保留用户输入的原始文本语义：能转数字就转，否则按字符串（类目/日期）。 */
  const coerce = (text: string): string | number => {
    const trimmed = text.trim();
    const n = Number(trimmed);
    return trimmed !== "" && Number.isFinite(n) ? n : trimmed;
  };

  return (
    <div
      data-testid="editor-panel-overlays"
      className="mt-2 rounded border border-slate-200 bg-slate-50/50 p-2"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[11px] font-medium text-slate-500">
          {t("console.dashboard.editor.overlaysTitle")}
        </span>
        <button
          type="button"
          data-testid="editor-overlay-add-line"
          onClick={() =>
            patchOverlays({
              ...overlays,
              markLine: [...lines, { value: 0, label: "" }],
            })
          }
          className="rounded border border-slate-300 bg-white px-1.5 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100"
        >
          {t("console.dashboard.editor.addThresholdLine")}
        </button>
        <button
          type="button"
          data-testid="editor-overlay-add-area"
          onClick={() =>
            patchOverlays({
              ...overlays,
              markArea: [...areas, { from: "", to: "", label: "" }],
            })
          }
          className="rounded border border-slate-300 bg-white px-1.5 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100"
        >
          {t("console.dashboard.editor.addArea")}
        </button>
      </div>

      {lines.length === 0 && areas.length === 0 && (
        <p className="mt-1 text-[11px] text-slate-400">
          {t("console.dashboard.editor.overlaysEmpty")}
        </p>
      )}

      {lines.map((line, index) => (
        <div
          key={`line-${index}`}
          data-testid="editor-overlay-line"
          className="mt-1.5 flex flex-wrap items-center gap-1.5"
        >
          <span className={labelClass}>
            {t("console.dashboard.editor.thresholdValue")}
          </span>
          <input
            data-testid="editor-overlay-line-value"
            value={String(line.value)}
            onChange={(e) => {
              const next = [...lines];
              next[index] = { ...line, value: coerce(e.target.value) };
              patchOverlays({ ...overlays, markLine: next });
            }}
            className={`${fieldClass} w-20`}
          />
          <input
            data-testid="editor-overlay-line-label"
            value={line.label ?? ""}
            placeholder={t("console.dashboard.editor.overlayLabel")}
            onChange={(e) => {
              const next = [...lines];
              next[index] = { ...line, label: e.target.value || undefined };
              patchOverlays({ ...overlays, markLine: next });
            }}
            className={`${fieldClass} w-32`}
          />
          <select
            data-testid="editor-overlay-line-style"
            value={line.lineStyle ?? "solid"}
            onChange={(e) => {
              const next = [...lines];
              next[index] = {
                ...line,
                lineStyle: e.target.value as typeof line.lineStyle,
              };
              patchOverlays({ ...overlays, markLine: next });
            }}
            className={fieldClass}
          >
            <option value="solid">{t("console.dashboard.lineStyle.solid")}</option>
            <option value="dashed">{t("console.dashboard.lineStyle.dashed")}</option>
            <option value="dotted">{t("console.dashboard.lineStyle.dotted")}</option>
          </select>
          <button
            type="button"
            data-testid="editor-overlay-line-remove"
            onClick={() =>
              patchOverlays({
                ...overlays,
                markLine: lines.filter((_, i) => i !== index),
              })
            }
            className="rounded border border-rose-200 bg-white px-1.5 py-0.5 text-[11px] text-rose-600 hover:bg-rose-50"
          >
            {t("console.dashboard.editor.remove")}
          </button>
        </div>
      ))}

      {areas.map((area, index) => (
        <div
          key={`area-${index}`}
          data-testid="editor-overlay-area"
          className="mt-1.5 flex flex-wrap items-center gap-1.5"
        >
          <span className={labelClass}>
            {t("console.dashboard.editor.areaFrom")}
          </span>
          <input
            data-testid="editor-overlay-area-from"
            value={String(area.from)}
            onChange={(e) => {
              const next = [...areas];
              next[index] = { ...area, from: coerce(e.target.value) };
              patchOverlays({ ...overlays, markArea: next });
            }}
            className={`${fieldClass} w-20`}
          />
          <span className={labelClass}>
            {t("console.dashboard.editor.areaTo")}
          </span>
          <input
            data-testid="editor-overlay-area-to"
            value={String(area.to)}
            onChange={(e) => {
              const next = [...areas];
              next[index] = { ...area, to: coerce(e.target.value) };
              patchOverlays({ ...overlays, markArea: next });
            }}
            className={`${fieldClass} w-20`}
          />
          <input
            data-testid="editor-overlay-area-label"
            value={area.label ?? ""}
            placeholder={t("console.dashboard.editor.overlayLabel")}
            onChange={(e) => {
              const next = [...areas];
              next[index] = { ...area, label: e.target.value || undefined };
              patchOverlays({ ...overlays, markArea: next });
            }}
            className={`${fieldClass} w-32`}
          />
          <button
            type="button"
            data-testid="editor-overlay-area-remove"
            onClick={() =>
              patchOverlays({
                ...overlays,
                markArea: areas.filter((_, i) => i !== index),
              })
            }
            className="rounded border border-rose-200 bg-white px-1.5 py-0.5 text-[11px] text-rose-600 hover:bg-rose-50"
          >
            {t("console.dashboard.editor.remove")}
          </button>
        </div>
      ))}
    </div>
  );
}

/** 面板高度表单（px；清空回落默认高度）。 */
export function PanelHeightForm({
  panel,
  onPatch,
}: {
  panel: DashboardPanel;
  onPatch: (updater: (panel: DashboardPanel) => DashboardPanel) => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="mt-2 flex flex-wrap items-center gap-2">
      <label className={labelClass} htmlFor={`height-${panel.id}`}>
        {t("console.dashboard.editor.heightLabel")}
      </label>
      <input
        id={`height-${panel.id}`}
        data-testid="editor-panel-height"
        type="number"
        min={120}
        step={20}
        value={panel.height ?? ""}
        placeholder="220"
        onChange={(e) => {
          const raw = e.target.value;
          onPatch((current) => {
            if (raw === "") {
              const { height: _drop, ...rest } = current;
              return rest;
            }
            const value = Number(raw);
            if (!Number.isFinite(value) || value <= 0) return current;
            return { ...current, height: Math.round(value) };
          });
        }}
        className={`${fieldClass} w-24`}
      />
      <span className="text-[11px] text-slate-400">
        {t("console.dashboard.editor.heightHint")}
      </span>
    </div>
  );
}

/** raw_option 面板的只读结构查看（不提供手写配置入口——design D8）。 */
export function RawOptionViewer({ panel }: { panel: DashboardPanel }) {
  const { t } = useTranslation();
  return (
    <div
      data-testid="editor-raw-option"
      className="mt-2 rounded border border-indigo-200 bg-indigo-50/40 p-2"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="rounded bg-indigo-100 px-1.5 py-0.5 text-[10px] font-medium text-indigo-700">
          {t("console.dashboard.rawOptionBadge")}
        </span>
        <span className="text-[11px] text-indigo-800">
          {t("console.dashboard.editor.rawOptionNote")}
        </span>
      </div>
      <pre
        data-testid="editor-raw-option-json"
        className="mt-1.5 max-h-48 overflow-auto whitespace-pre-wrap break-all rounded bg-white p-2 font-mono text-[10px] text-slate-600"
      >
        {JSON.stringify(panel.raw_option ?? {}, null, 2)}
      </pre>
    </div>
  );
}

/** 面板是否显示笛卡尔编码表单（无轴类型不显示）。 */
export function showsEncodingForm(panel: DashboardPanel): boolean {
  if (panel.raw_option !== undefined) return false;
  return CARTESIAN_UI_TYPES.has(panel.type);
}

/** 面板是否显示叠加层表单（笛卡尔与无轴类型都支持叠加层）。 */
/** 列表面板的角色通道（wire-list-panel）：title 必填，其余可选。 */
const LIST_ROLES = [
  "title",
  "summary",
  "image",
  "time",
  "source",
  "link",
] as const;
type ListRole = (typeof LIST_ROLES)[number];

export function ListEncodingForm({
  panel,
  onPatch,
}: {
  panel: DashboardPanel;
  onPatch: (updater: (panel: DashboardPanel) => DashboardPanel) => void;
}) {
  const { t } = useTranslation();
  const columns = panel.snapshot?.columns ?? [];
  const encoding: DashboardPanelEncoding = panel.encoding ?? {};

  const patchRole = (role: ListRole, value: string | undefined) => {
    onPatch((current) => {
      const nextEncoding: DashboardPanelEncoding = { ...current.encoding };
      if (value === undefined || value === '') {
        if (role === 'title') {
          return current; // Title is required, cannot unset
        }
        delete nextEncoding[role];
      } else {
        nextEncoding[role] = value;
      }
      return { ...current, encoding: nextEncoding };
    });
  };

  const patchLimit = (value: number) => {
    onPatch((current) => ({
      ...current,
      config: { ...current.config, limit: value },
    }));
  };

  return (
    <div
      data-testid="editor-panel-list-encoding"
      className="mt-2 rounded border border-slate-200 bg-slate-50/50 p-2"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[11px] font-medium text-slate-500">
          {t("console.dashboard.editor.listForm.title")}
        </span>
        {columns.length === 0 && (
          <span className={labelClass}>
            {t("console.dashboard.editor.listForm.needsData")}
          </span>
        )}
      </div>

      {columns.length > 0 && (
        <>
          {LIST_ROLES.map((role) => (
            <div key={role} className="mt-1.5 flex flex-col">
              <label
                className={labelClass}
                htmlFor={`list-role-${role}-${panel.id}`}
              >
                {t(`console.dashboard.editor.listForm.${role}Role`)}
              </label>
              <select
                id={`list-role-${role}-${panel.id}`}
                data-testid={`editor-list-role-${role}`}
                value={encoding[role] ?? ''}
                onChange={(e) => {
                  const value = e.target.value;
                  patchRole(
                    role,
                    value === '' ? undefined : value,
                  );
                }}
                className={fieldClass}
              >
                {role !== 'title' && (
                  <option value="">
                    {t("console.dashboard.editor.listForm.notSet")}
                  </option>
                )}
                {columns.map((column) => (
                  <option key={column} value={column}>
                    {column}
                  </option>
                ))}
              </select>
            </div>
          ))}

          <div className="mt-1.5 flex flex-wrap items-center gap-2">
            <label
              className={labelClass}
              htmlFor={`list-limit-${panel.id}`}
            >
              {t("console.dashboard.editor.listForm.limit")}
            </label>
            <input
              id={`list-limit-${panel.id}`}
              data-testid="editor-list-limit"
              type="number"
              min={1}
              max={20}
              value={panel.config?.limit ?? 20}
              onChange={(e) => {
                let value = e.target.value.trim();
                if (value === '') {
                  patchLimit(20);
                  return;
                }
                const num = parseInt(value, 10);
                const clamped = isNaN(num)
                  ? 20
                  : Math.max(1, Math.min(20, num));
                patchLimit(clamped);
              }}
              className={`${fieldClass} w-20`}
            />
          </div>

          <p className="mt-1 text-[11px] text-slate-400">
            {t("console.dashboard.editor.listForm.hint")}
          </p>
        </>
      )}
    </div>
  );
}

export function showsListEncodingForm(panel: DashboardPanel): boolean {
  return panel.type === "list" && panel.raw_option === undefined;
}

export function showsOverlaysForm(panel: DashboardPanel): boolean {
  return panel.raw_option === undefined && panel.type !== "list";
}
