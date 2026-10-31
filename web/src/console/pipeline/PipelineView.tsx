// 数据页「管线」子视图（wire-transform-pipeline-view 任务 2.1/2.2 起，
// wire-transform-pipelines 任务 5.1-5.4 升级为双页签工作台）：
// - 「管线」页签：管线工作台——列表（名称/版本徽标/步骤数/调度 cron 与
//   启停态/最近运行状态章+触发方式+时间）→ 新建/编辑（步骤链编辑、
//   版本语义明示）→ 详情（立即运行+逐步骤结果+运行历史分页、cron 调度
//   挂接/暂停/恢复/删除、版本历史 append-only 可查）；
// - 「图谱」页签：既有只读派生血缘图（PipelineGraphView，原样保留）。
// 点击输出数据集跳数据集详情（onOpenDataset 回调，与列表卡同导航方式）。
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  apiErrorText,
  fetchPipeline,
  fetchPipelines,
  fetchDatasets,
  type PipelineDetail,
  type PipelineListItem,
  type PipelinePage,
  type PipelineScheduleInfo,
  type PipelineUpdateResult,
} from "../api";
import { PipelineGraphView, formatShortServerDate } from "./PipelineGraphView";
import { PipelineEditor, type DatasetOption } from "./PipelineEditor";
import { PipelineRunPanel } from "./PipelineRunPanel";
import { PipelineScheduleSection } from "./PipelineScheduleSection";

/** 运行状态章（列表行内小号版；success/failed 双态）。 */
function RunChip({ status }: { status: "success" | "failed" }) {
  const { t } = useTranslation();
  return (
    <span
      className={`rounded-full px-2 py-0.5 text-[11px] ring-1 ${
        status === "success"
          ? "bg-emerald-50 text-emerald-700 ring-emerald-200"
          : "bg-red-50 text-red-700 ring-red-200"
      }`}
    >
      {t(`console.datasets.pipeline.workbench.runStatus.${status}`)}
    </span>
  );
}

/** 工作台子模式：列表 / 编辑器（新建 pipelineId=null）/ 详情。 */
type WorkbenchMode =
  | { kind: "list" }
  | { kind: "editor"; pipelineId: number | null }
  | { kind: "detail"; pipelineId: number };

export function PipelineView({
  onOpenDataset,
}: {
  /** 点击输出数据集跳数据集详情（DatasetsPage 以 setSelectedId 接线）。 */
  onOpenDataset: (datasetId: string) => void;
}) {
  const { t } = useTranslation();
  const [tab, setTab] = useState<"workbench" | "graph">("workbench");
  const [mode, setMode] = useState<WorkbenchMode>({ kind: "list" });

  return (
    <section data-testid="pipeline-view" className="mt-2">
      {/* 双页签：「管线」工作台 + 「图谱」只读视图（分段切换器同数据页风格）。 */}
      <div
        data-testid="pipeline-tabs"
        className="inline-flex items-center gap-1 rounded-lg border border-slate-200 bg-slate-50/60 p-1"
        role="tablist"
      >
        <button
          type="button"
          role="tab"
          aria-selected={tab === "workbench"}
          data-testid="pipeline-tab-workbench"
          onClick={() => setTab("workbench")}
          className={`rounded-md px-3 py-1 text-xs transition-colors ${
            tab === "workbench"
              ? "bg-slate-900 text-white"
              : "text-slate-600 hover:bg-slate-100"
          }`}
        >
          {t("console.datasets.pipeline.workbench.tab")}
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "graph"}
          data-testid="pipeline-tab-graph"
          onClick={() => setTab("graph")}
          className={`rounded-md px-3 py-1 text-xs transition-colors ${
            tab === "graph"
              ? "bg-slate-900 text-white"
              : "text-slate-600 hover:bg-slate-100"
          }`}
        >
          {t("console.datasets.pipeline.workbench.graphTab")}
        </button>
      </div>

      {tab === "graph" ? (
        <PipelineGraphView onOpenDataset={onOpenDataset} />
      ) : mode.kind === "list" ? (
        <PipelineWorkbenchList
          onOpen={(id) => setMode({ kind: "detail", pipelineId: id })}
          onCreate={() => setMode({ kind: "editor", pipelineId: null })}
        />
      ) : mode.kind === "editor" ? (
        <PipelineEditorScreen
          pipelineId={mode.pipelineId}
          onSaved={(result) =>
            setMode({ kind: "detail", pipelineId: result.id })
          }
          onCancel={() => setMode({ kind: "list" })}
        />
      ) : (
        <PipelineDetailScreen
          pipelineId={mode.pipelineId}
          onBack={() => setMode({ kind: "list" })}
          onEdit={() => setMode({ kind: "editor", pipelineId: mode.pipelineId })}
          onOpenDataset={onOpenDataset}
        />
      )}
    </section>
  );
}

/** 管线列表（5.1）：名称/版本徽标/步骤数/调度态/最近运行。 */
function PipelineWorkbenchList({
  onOpen,
  onCreate,
}: {
  onOpen: (id: number) => void;
  onCreate: () => void;
}) {
  const { t } = useTranslation();
  const [page, setPage] = useState<PipelinePage | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setPage(await fetchPipelines());
    } catch (err) {
      setPage(null);
      setError(apiErrorText(err, t("console.datasets.pipeline.errorTitle")));
    }
  }, [t]);

  useEffect(() => {
    setPage(null);
    void load();
  }, [load]);

  return (
    <div data-testid="pipeline-workbench" className="mt-2">
      <div className="flex flex-wrap items-center gap-3">
        <p className="max-w-2xl text-xs text-slate-500">
          {t("console.datasets.pipeline.workbench.subtitle")}
        </p>
        <button
          type="button"
          data-testid="pipeline-create"
          onClick={onCreate}
          className="ml-auto rounded-md bg-slate-900 px-3 py-1.5 text-xs text-white hover:bg-slate-700"
        >
          {t("console.datasets.pipeline.workbench.create")}
        </button>
      </div>

      {error != null && (
        <div
          data-testid="pipeline-list-error"
          className="mt-4 rounded-md border border-red-200 bg-red-50 p-4"
        >
          <p className="text-sm text-red-700">
            {t("console.datasets.pipeline.errorTitle")}
          </p>
          {error !== t("console.datasets.pipeline.errorTitle") && (
            <p className="mt-1 break-all font-mono text-[11px] text-red-500">
              {error}
            </p>
          )}
          <button
            type="button"
            data-testid="pipeline-list-retry"
            onClick={() => void load()}
            className="mt-2 rounded border border-red-300 px-2 py-0.5 text-xs text-red-700 hover:bg-red-100"
          >
            {t("console.datasets.pipeline.retry")}
          </button>
        </div>
      )}

      {page == null && error == null && (
        <p
          data-testid="pipeline-list-loading"
          className="mt-4 text-xs text-slate-400"
        >
          {t("console.datasets.pipeline.workbench.loading")}
        </p>
      )}

      {page != null && page.items.length === 0 && (
        <div
          data-testid="pipeline-list-empty"
          className="mt-4 rounded-lg border border-dashed border-slate-300 p-8 text-center"
        >
          <p className="text-sm text-slate-600">
            {t("console.datasets.pipeline.workbench.empty")}
          </p>
          <p className="mt-1 text-xs text-slate-400">
            {t("console.datasets.pipeline.workbench.emptyHint")}
          </p>
        </div>
      )}

      {page != null && page.items.length > 0 && (
        <ul data-testid="pipeline-list" className="mt-3 space-y-2">
          {page.items.map((item) => (
            <PipelineRow key={item.id} item={item} onOpen={() => onOpen(item.id)} />
          ))}
        </ul>
      )}
    </div>
  );
}

function PipelineRow({ item, onOpen }: { item: PipelineListItem; onOpen: () => void }) {
  const { t } = useTranslation();
  return (
    <li>
      <button
        type="button"
        data-testid="pipeline-row"
        data-pipeline-id={item.id}
        onClick={onOpen}
        className="w-full rounded-lg border border-slate-200 bg-white p-4 text-left transition-colors hover:border-slate-400"
      >
        <div className="flex flex-wrap items-center gap-2">
          <span
            data-testid="pipeline-row-name"
            className="text-sm font-medium text-slate-800"
          >
            {item.name}
          </span>
          <span
            data-testid="pipeline-row-version"
            data-version={item.current_version}
            className="rounded-full bg-slate-900 px-2 py-0.5 font-mono text-[11px] text-white"
          >
            v{item.current_version}
          </span>
          <span
            data-testid="pipeline-row-steps"
            className="text-[11px] text-slate-500"
          >
            {t("console.datasets.pipeline.workbench.stepsCount", {
              count: item.steps_count,
            })}
          </span>
          {item.description && (
            <span className="truncate text-[11px] text-slate-400">
              {item.description}
            </span>
          )}
        </div>
        <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-slate-500">
          <span data-testid="pipeline-row-base">
            {t("console.datasets.pipeline.workbench.baseLabel")}：
            {item.base_dataset_name}
          </span>
          {item.schedule ? (
            <span
              data-testid="pipeline-row-schedule"
              data-cron={item.schedule.cron}
              data-enabled={item.schedule.enabled ? "true" : "false"}
              className={`rounded px-1.5 py-0.5 font-mono ${
                item.schedule.enabled
                  ? "bg-sky-50 text-sky-700"
                  : "bg-slate-100 text-slate-500"
              }`}
              title={item.schedule.cron}
            >
              {item.schedule.cron}
              {!item.schedule.enabled &&
                ` · ${t("console.datasets.pipeline.workbench.schedulePaused")}`}
            </span>
          ) : (
            <span data-testid="pipeline-row-noschedule">
              {t("console.datasets.pipeline.workbench.noSchedule")}
            </span>
          )}
          {item.last_run ? (
            <span
              data-testid="pipeline-row-lastrun"
              data-status={item.last_run.status}
              data-trigger={item.last_run.trigger}
              className="flex items-center gap-1.5"
            >
              <RunChip status={item.last_run.status} />
              <span>
                {item.last_run.trigger === "schedule"
                  ? t("console.datasets.pipeline.workbench.trigger.schedule")
                  : t("console.datasets.pipeline.workbench.trigger.manual")}
              </span>
              <span>{formatShortServerDate(item.last_run.started_at)}</span>
            </span>
          ) : (
            <span data-testid="pipeline-row-norun">
              {t("console.datasets.pipeline.workbench.neverRun")}
            </span>
          )}
        </div>
      </button>
    </li>
  );
}

/** 编辑器屏幕：编辑既有管线时先拉详情（步骤草稿取当前版本）。 */
function PipelineEditorScreen({
  pipelineId,
  onSaved,
  onCancel,
}: {
  pipelineId: number | null;
  onSaved: (result: PipelineUpdateResult) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [pipeline, setPipeline] = useState<PipelineDetail | null>(null);
  const [datasets, setDatasets] = useState<DatasetOption[] | null>(null);
  const [loadError, setLoadError] = useState(false);

  const load = useCallback(async () => {
    setLoadError(false);
    try {
      // 详情与数据集候选并行：任一失败整屏错误态（编辑器两端都依赖）。
      const [detail, datasetList] = await Promise.all([
        pipelineId == null ? Promise.resolve(null) : fetchPipeline(pipelineId),
        fetchDatasets().catch(() => []),
      ]);
      setPipeline(detail);
      // key/columns 供窗口步骤的分区列多选与试运行占位声明（wire-window-operator）。
      setDatasets(
        datasetList.map((d) => ({
          id: d.id,
          name: d.name,
          key: d.key,
          columns: d.columns,
        })),
      );
    } catch {
      setLoadError(true);
    }
  }, [pipelineId]);

  useEffect(() => {
    setPipeline(null);
    setDatasets(null);
    void load();
  }, [load]);

  if (loadError) {
    return (
      <div data-testid="pipeline-editor-load-error" className="mt-4">
        <p className="text-xs text-red-600">
          {t("console.datasets.pipeline.workbench.loadError")}
        </p>
        <button
          type="button"
          data-testid="pipeline-editor-load-retry"
          onClick={() => void load()}
          className="mt-1 rounded border border-red-300 px-2 py-0.5 text-xs text-red-700 hover:bg-red-100"
        >
          {t("console.datasets.pipeline.retry")}
        </button>
      </div>
    );
  }

  if (datasets == null) {
    return (
      <p data-testid="pipeline-editor-loading" className="mt-4 text-xs text-slate-400">
        {t("console.datasets.pipeline.workbench.loading")}
      </p>
    );
  }

  return (
    <PipelineEditor
      pipeline={pipelineId == null ? null : pipeline}
      datasets={datasets}
      onSaved={onSaved}
      onCancel={onCancel}
    />
  );
}

/** 管线详情（5.3/5.4 载体）：头部信息 + 版本历史 + 调度小节 + 运行面板。 */
function PipelineDetailScreen({
  pipelineId,
  onBack,
  onEdit,
  onOpenDataset,
}: {
  pipelineId: number;
  onBack: () => void;
  onEdit: () => void;
  onOpenDataset: (datasetId: string) => void;
}) {
  const { t } = useTranslation();
  const [detail, setDetail] = useState<PipelineDetail | null>(null);
  const [loadError, setLoadError] = useState(false);
  // 调度态（「输出已由管线调度管理」提示依据；由调度小节加载/变更时同步）。
  const [schedule, setSchedule] = useState<PipelineScheduleInfo | null>(null);

  const load = useCallback(async () => {
    setLoadError(false);
    try {
      setDetail(await fetchPipeline(pipelineId));
    } catch {
      setDetail(null);
      setLoadError(true);
    }
  }, [pipelineId]);

  useEffect(() => {
    setDetail(null);
    setSchedule(null);
    void load();
  }, [load]);

  if (loadError) {
    return (
      <div data-testid="pipeline-detail-error" className="mt-4">
        <p className="text-xs text-red-600">
          {t("console.datasets.pipeline.workbench.loadError")}
        </p>
        <button
          type="button"
          data-testid="pipeline-detail-retry"
          onClick={() => void load()}
          className="mt-1 rounded border border-red-300 px-2 py-0.5 text-xs text-red-700 hover:bg-red-100"
        >
          {t("console.datasets.pipeline.retry")}
        </button>
      </div>
    );
  }

  if (detail == null) {
    return (
      <p data-testid="pipeline-detail-loading" className="mt-4 text-xs text-slate-400">
        {t("console.datasets.pipeline.workbench.loading")}
      </p>
    );
  }

  const currentVersion =
    detail.versions.find((v) => v.version === detail.current_version) ??
    detail.versions[detail.versions.length - 1];

  return (
    <div data-testid="pipeline-detail" data-pipeline-id={detail.id} className="mt-2">
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          data-testid="pipeline-detail-back"
          onClick={onBack}
          className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50"
        >
          ← {t("console.datasets.pipeline.workbench.backToList")}
        </button>
        <h3
          data-testid="pipeline-detail-name"
          className="text-sm font-semibold text-slate-800"
        >
          {detail.name}
        </h3>
        <span
          data-testid="pipeline-detail-version"
          data-version={detail.current_version}
          className="rounded-full bg-slate-900 px-2 py-0.5 font-mono text-[11px] text-white"
        >
          v{detail.current_version}
        </span>
        <button
          type="button"
          data-testid="pipeline-edit"
          onClick={onEdit}
          className="ml-auto rounded border border-slate-300 px-2.5 py-1 text-xs text-slate-600 hover:bg-slate-50"
        >
          {t("console.datasets.pipeline.workbench.edit")}
        </button>
      </div>

      {detail.description && (
        <p className="mt-1.5 max-w-2xl text-xs text-slate-500">
          {detail.description}
        </p>
      )}
      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-slate-500">
        <span data-testid="pipeline-detail-base">
          {t("console.datasets.pipeline.workbench.baseLabel")}：
          {detail.base_dataset_name}
        </span>
        <span data-testid="pipeline-detail-updated">
          {t("console.datasets.pipeline.updatedLabel")}{" "}
          {formatShortServerDate(detail.updated_at)}
        </span>
      </div>

      {/* 已挂调度：输出由管线调度管理（不再手工重跑也保持新鲜）。 */}
      {schedule != null && (
        <p
          data-testid="pipeline-schedule-managed-note"
          className="mt-2 max-w-2xl rounded border border-sky-200 bg-sky-50/60 px-3 py-2 text-xs text-sky-700"
        >
          {t("console.datasets.pipeline.workbench.scheduleManagedNote")}
        </p>
      )}

      {/* 版本历史（append-only）：当前版本步骤链 + 旧版本原样可查。 */}
      <section data-testid="pipeline-versions" className="mt-5">
        <h4 className="text-xs font-medium text-slate-600">
          {t("console.datasets.pipeline.workbench.versionsTitle")}
        </h4>
        <ul className="mt-2 space-y-2">
          {[...detail.versions].reverse().map((version) => (
            <li
              key={version.version}
              data-testid="pipeline-version"
              data-version={version.version}
              data-current={version.version === detail.current_version ? "true" : "false"}
              className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs"
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-700">
                  v{version.version}
                </span>
                {version.version === detail.current_version && (
                  <span
                    data-testid={`pipeline-version-current-${version.version}`}
                    className="rounded bg-teal-50 px-1.5 py-0.5 text-[11px] text-teal-700"
                  >
                    {t("console.datasets.pipeline.workbench.currentVersion")}
                  </span>
                )}
                <span className="text-[11px] text-slate-400">
                  {t("console.datasets.pipeline.workbench.versionMeta", {
                    time: formatShortServerDate(version.created_at),
                    actor: version.created_by,
                  })}
                </span>
              </div>
              <ol className="mt-1.5 flex flex-wrap gap-1.5">
                {version.steps.map((step, index) => (
                  <li
                    key={`${version.version}-${index}`}
                    data-testid="pipeline-version-step"
                    className="rounded border border-slate-200 bg-slate-50 px-1.5 py-0.5 text-[11px] text-slate-600"
                  >
                    {index + 1}.
                    {t(`console.datasets.pipeline.workbench.opLabels.${step.op}`)}
                    {step.name ? `·${step.name}` : ""}
                  </li>
                ))}
              </ol>
            </li>
          ))}
        </ul>
        {currentVersion && currentVersion.steps.length === 0 && (
          <p className="mt-1 text-[11px] text-slate-400">
            {t("console.datasets.pipeline.workbench.stepsEmpty")}
          </p>
        )}
      </section>

      <PipelineScheduleSection
        pipelineId={pipelineId}
        onScheduleChanged={setSchedule}
      />

      <PipelineRunPanel pipelineId={pipelineId} onOpenDataset={onOpenDataset} />
    </div>
  );
}
