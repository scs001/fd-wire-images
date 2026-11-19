// 管线运行视图（wire-transform-pipelines 任务 5.3；wire-dag-executor D7 扩展）：
// 「立即运行」（in-flight 禁用 + 运行中文案）+ 本次运行结果面板（逐步骤卡片
// 按序：op 标签/名称/状态章 success·failed·skipped·quota_exhausted 四色区分/
// 行数/耗时/输出数据集跳转详情；失败步骤如实呈现错误与「后续步骤未执行」
// 注记——失败后的步骤没有记录，绝不伪造）+ 运行历史（倒序分页，点击展开该次
// run 到结果面板）。触发方式 manual/schedule 如实标注。
// DAG 扩展（D7）：响应含 `nodes` 时额外渲染只读节点图（状态色 queued 灰/
// running 蓝/succeeded 绿/failed 红/skipped 虚），点击节点看耗时/行数/错误；
// run 处于 queued/running 时每 2s 轮询详情（收敛后自动停）；queued/running
// 的 run 显示「取消运行」（cancel_requested_at 已置时置灰并如实说明语义）。
// 无 `nodes` 的旧响应仍走步骤表（向后兼容）。
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  apiErrorText,
  cancelRun,
  fetchPipelineRun,
  fetchPipelineRuns,
  runPipeline,
  type PipelineNodeStatus,
  type PipelineRunDetail,
  type PipelineRunStatus,
  type PipelineRunSummary,
  type PipelineStepRun,
} from "../api";
import { formatShortServerDate } from "./PipelineGraphView";

const HISTORY_PAGE_SIZE = 10;

/** 在飞 run 的轮询间隔（D7：2s）。 */
export const RUN_POLL_MS = 2000;

/** 步骤状态章配色：四态四调（配额耗尽=rose，与失败 red 区分）。 */
const STEP_STATUS_CLASS: Record<PipelineStepRun["status"], string> = {
  success: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  failed: "bg-red-50 text-red-700 ring-red-200",
  skipped: "bg-slate-100 text-slate-600 ring-slate-200",
  quota_exhausted: "bg-rose-50 text-rose-700 ring-rose-200",
};

/** run 状态章配色：queued 灰 / running 蓝 / success 绿 / failed 红 /
 *  cancelled 琥珀（取消如实标注，不与失败混淆）。 */
const RUN_STATUS_CLASS: Record<PipelineRunStatus, string> = {
  queued: "bg-slate-100 text-slate-600 ring-slate-200",
  running: "bg-sky-50 text-sky-700 ring-sky-200",
  success: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  failed: "bg-red-50 text-red-700 ring-red-200",
  cancelled: "bg-amber-50 text-amber-700 ring-amber-200",
};

/** 节点状态配色（D7 逐字）：queued 灰 / running 蓝 / succeeded 绿 /
 *  failed 红 / skipped 虚线（border-dashed，非实色填充）。 */
const NODE_STATUS_CLASS: Record<PipelineNodeStatus, string> = {
  queued: "border-slate-300 bg-slate-100 text-slate-600",
  running: "border-sky-400 bg-sky-50 text-sky-700",
  succeeded: "border-emerald-400 bg-emerald-50 text-emerald-700",
  failed: "border-red-400 bg-red-50 text-red-700",
  skipped: "border-dashed border-slate-400 bg-white text-slate-500",
};

function isActiveRun(status: PipelineRunStatus): boolean {
  return status === "queued" || status === "running";
}

function RunStatusChip({ status }: { status: PipelineRunStatus }) {
  const { t } = useTranslation();
  return (
    <span
      data-testid="pipeline-run-status"
      data-status={status}
      className={`rounded-full px-2 py-0.5 text-xs ring-1 ${
        RUN_STATUS_CLASS[status] ?? "bg-slate-100 text-slate-600 ring-slate-200"
      }`}
    >
      {t(`console.datasets.pipeline.workbench.runStatus.${status}`)}
    </span>
  );
}

function triggerLabel(t: (key: string) => string, trigger: string): string {
  return trigger === "schedule"
    ? t("console.datasets.pipeline.workbench.trigger.schedule")
    : t("console.datasets.pipeline.workbench.trigger.manual");
}

/** 本次运行结果面板（run 摘要 + 节点图（有 nodes 时）+ 逐步骤卡片；
 *  失败中止注记；在飞 run 的取消按钮）。 */
function RunResult({
  run,
  onOpenDataset,
  onCancel,
  cancelling,
}: {
  run: PipelineRunDetail;
  onOpenDataset: (datasetId: string) => void;
  onCancel: (runId: number) => void;
  cancelling: boolean;
}) {
  const { t } = useTranslation();
  const failedSeq = run.steps.find((s) => s.status !== "success")?.seq;
  const nodes = run.nodes ?? [];
  const [selectedNodeKey, setSelectedNodeKey] = useState<string | null>(null);
  const selectedNode =
    selectedNodeKey == null
      ? null
      : (nodes.find((node) => node.node_key === selectedNodeKey) ?? null);
  // 切换所看 run 时清空节点选中（节点集合不同，不跨 run 残留）。
  useEffect(() => {
    setSelectedNodeKey(null);
  }, [run.id]);
  const active = isActiveRun(run.status);
  const cancelRequested = run.cancel_requested_at != null;
  return (
    <div
      data-testid="pipeline-run-result"
      data-run-id={run.id}
      data-status={run.status}
      data-version={run.version}
      data-trigger={run.trigger}
      className="mt-3 rounded-lg border border-slate-200 bg-white p-4"
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
        <h4 className="text-sm font-semibold text-slate-800">
          {t("console.datasets.pipeline.workbench.runTitle")}
        </h4>
        <RunStatusChip status={run.status} />
        <span
          data-testid="pipeline-run-version"
          className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-600"
        >
          v{run.version}
        </span>
        <span
          data-testid="pipeline-run-trigger"
          data-trigger={run.trigger}
          className="rounded bg-sky-50 px-1.5 py-0.5 text-[11px] text-sky-700"
        >
          {triggerLabel(t, run.trigger)}
        </span>
        <span data-testid="pipeline-run-time" className="text-slate-500">
          {formatShortServerDate(run.started_at)}
          {run.finished_at ? ` → ${formatShortServerDate(run.finished_at)}` : ""}
        </span>
        {active && (
          <button
            type="button"
            data-testid="pipeline-run-cancel"
            disabled={cancelling || cancelRequested}
            onClick={() => onCancel(run.id)}
            className="ml-auto rounded border border-red-300 px-2 py-0.5 text-[11px] text-red-600 hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {cancelRequested
              ? t("console.datasets.pipeline.workbench.cancelRequested")
              : cancelling
                ? t("console.datasets.pipeline.workbench.cancelling")
                : t("console.datasets.pipeline.workbench.cancelRun")}
          </button>
        )}
      </div>
      {cancelRequested && (
        <p
          data-testid="pipeline-run-cancel-note"
          className="mt-2 text-[11px] text-amber-700"
        >
          {t("console.datasets.pipeline.workbench.cancelRequestedNote")}
        </p>
      )}
      {active && !cancelRequested && (
        <p
          data-testid="pipeline-run-polling-note"
          className="mt-2 text-[11px] text-slate-400"
        >
          {t("console.datasets.pipeline.workbench.pollingNote")}
        </p>
      )}
      {run.error_text && (
        <p
          data-testid="pipeline-run-error-text"
          className="mt-2 break-all font-mono text-[11px] text-red-500"
        >
          {run.error_text}
        </p>
      )}
      {nodes.length > 0 && (
        <div data-testid="pipeline-run-node-graph" className="mt-3">
          <div className="flex flex-wrap items-baseline gap-2">
            <h5 className="text-xs font-medium text-slate-600">
              {t("console.datasets.pipeline.workbench.nodeGraphTitle")}
            </h5>
            <span className="text-[11px] text-slate-400">
              {t("console.datasets.pipeline.workbench.nodeGraphHint")}
            </span>
          </div>
          <ol className="mt-2 flex flex-wrap items-center gap-2">
            {nodes.map((node, index) => (
              <li key={node.node_key} className="flex items-center gap-2">
                {index > 0 && (
                  <span aria-hidden="true" className="text-slate-300">
                    →
                  </span>
                )}
                <button
                  type="button"
                  data-testid="pipeline-run-node"
                  data-node-key={node.node_key}
                  data-status={node.status}
                  aria-pressed={selectedNodeKey === node.node_key}
                  onClick={() =>
                    setSelectedNodeKey((current) =>
                      current === node.node_key ? null : node.node_key,
                    )
                  }
                  className={`flex flex-col items-start gap-0.5 rounded-lg border px-2.5 py-1.5 text-left text-[11px] ${
                    NODE_STATUS_CLASS[node.status] ??
                    "border-slate-300 bg-slate-100 text-slate-600"
                  }`}
                >
                  <span
                    data-testid="pipeline-run-node-key"
                    className="font-medium"
                  >
                    {node.node_key}
                  </span>
                  <span className="flex items-center gap-1.5 text-[10px] opacity-80">
                    <span>
                      {t(
                        `console.datasets.pipeline.workbench.opLabels.${node.op}`,
                      )}
                    </span>
                    <span data-testid="pipeline-run-node-status">
                      {t(
                        `console.datasets.pipeline.workbench.nodeStatus.${node.status}`,
                      )}
                    </span>
                  </span>
                </button>
              </li>
            ))}
          </ol>
          {selectedNode && (
            <div
              data-testid="pipeline-run-node-detail"
              data-node-key={selectedNode.node_key}
              data-status={selectedNode.status}
              className="mt-2 rounded-lg border border-slate-200 bg-slate-50/70 p-2.5 text-[11px] text-slate-600"
            >
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <span className="font-medium text-slate-800">
                  {selectedNode.node_key}
                </span>
                <span
                  data-testid="pipeline-run-node-attempts"
                  className="text-slate-500"
                >
                  {t("console.datasets.pipeline.workbench.nodeAttempts", {
                    count: selectedNode.attempts,
                  })}
                </span>
                <span data-testid="pipeline-run-node-duration">
                  {selectedNode.duration_ms != null
                    ? t("console.datasets.pipeline.workbench.duration", {
                        ms: selectedNode.duration_ms,
                      })
                    : t("console.datasets.pipeline.workbench.nodeNoDuration")}
                </span>
                <span data-testid="pipeline-run-node-rows">
                  {selectedNode.row_count != null
                    ? t("console.datasets.pipeline.workbench.rows", {
                        count: selectedNode.row_count,
                      })
                    : t("console.datasets.pipeline.workbench.nodeNoRows")}
                </span>
              </div>
              <div className="mt-1 flex flex-wrap gap-x-3 text-[10px] text-slate-400">
                <span data-testid="pipeline-run-node-started">
                  {t("console.datasets.pipeline.workbench.nodeStartedAt", {
                    time: formatShortServerDate(selectedNode.started_at),
                  })}
                </span>
                <span data-testid="pipeline-run-node-finished">
                  {t("console.datasets.pipeline.workbench.nodeFinishedAt", {
                    time: formatShortServerDate(selectedNode.finished_at),
                  })}
                </span>
              </div>
              {selectedNode.error_text && (
                <p
                  data-testid="pipeline-run-node-error"
                  className="mt-1 break-all font-mono text-[11px] text-red-500"
                >
                  {selectedNode.error_text}
                </p>
              )}
            </div>
          )}
        </div>
      )}
      <ol className="mt-3 space-y-2">
        {run.steps.map((step) => (
          <li
            key={step.seq}
            data-testid="pipeline-run-step"
            data-seq={step.seq}
            data-op={step.op}
            data-status={step.status}
            className={`rounded-lg border p-3 ${
              step.status === "success"
                ? "border-slate-200"
                : "border-red-200 bg-red-50/40"
            }`}
          >
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <span className="flex h-5 w-5 items-center justify-center rounded-full bg-slate-900 text-[11px] font-medium text-white">
                {step.seq}
              </span>
              <span
                data-testid="pipeline-run-step-op"
                className="rounded bg-teal-50 px-1.5 py-0.5 text-[11px] text-teal-700"
              >
                {t(`console.datasets.pipeline.workbench.opLabels.${step.op}`)}
              </span>
              <span
                data-testid="pipeline-run-step-name"
                className="font-medium text-slate-800"
              >
                {step.name}
              </span>
              <span
                data-testid="pipeline-run-step-status"
                data-status={step.status}
                className={`rounded-full px-2 py-0.5 text-[11px] ring-1 ${
                  STEP_STATUS_CLASS[step.status] ??
                  "bg-slate-100 text-slate-600 ring-slate-200"
                }`}
              >
                {t(
                  `console.datasets.pipeline.workbench.stepStatus.${step.status}`,
                )}
              </span>
              {step.row_count != null && (
                <span
                  data-testid="pipeline-run-step-rows"
                  className="text-slate-500"
                >
                  {t("console.datasets.pipeline.workbench.rows", {
                    count: step.row_count,
                  })}
                </span>
              )}
              {step.duration_ms != null && (
                <span
                  data-testid="pipeline-run-step-duration"
                  className="text-slate-500"
                >
                  {t("console.datasets.pipeline.workbench.duration", {
                    ms: step.duration_ms,
                  })}
                </span>
              )}
              {step.output_dataset_id && (
                <button
                  type="button"
                  data-testid="pipeline-run-step-output"
                  data-dataset-id={step.output_dataset_id}
                  onClick={() => onOpenDataset(step.output_dataset_id!)}
                  className="ml-auto rounded border border-slate-300 px-2 py-0.5 text-[11px] text-slate-600 hover:bg-slate-50"
                >
                  {t("console.datasets.pipeline.workbench.openDataset", {
                    name: step.output_dataset_name ?? step.output_dataset_id,
                  })}
                </button>
              )}
            </div>
            {step.error_text && (
              <p
                data-testid="pipeline-run-step-error"
                className="mt-1.5 break-all font-mono text-[11px] text-red-500"
              >
                {step.error_text}
              </p>
            )}
            {/* 失败隔离如实：失败步骤后注记「后续步骤未执行」（后续无记录）。 */}
            {failedSeq === step.seq && step.status !== "success" && (
              <p
                data-testid="pipeline-run-aborted-note"
                className="mt-1.5 text-[11px] text-amber-700"
              >
                {t("console.datasets.pipeline.workbench.abortedNote")}
              </p>
            )}
          </li>
        ))}
      </ol>
    </div>
  );
}

export function PipelineRunPanel({
  pipelineId,
  onOpenDataset,
  pollIntervalMs = RUN_POLL_MS,
}: {
  pipelineId: number;
  onOpenDataset: (datasetId: string) => void;
  /** 在飞 run 轮询间隔（缺省 2s；测试可注入短间隔，语义不变）。 */
  pollIntervalMs?: number;
}) {
  const { t } = useTranslation();
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const [result, setResult] = useState<PipelineRunDetail | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const [history, setHistory] = useState<{
    items: PipelineRunSummary[];
    total: number;
  } | null>(null);
  const [historyError, setHistoryError] = useState(false);
  const [page, setPage] = useState(1);

  const loadHistory = useCallback(async () => {
    setHistoryError(false);
    try {
      setHistory(await fetchPipelineRuns(pipelineId, page, HISTORY_PAGE_SIZE));
    } catch {
      setHistory(null);
      setHistoryError(true);
    }
  }, [pipelineId, page]);

  useEffect(() => {
    void loadHistory();
  }, [loadHistory]);

  // 在飞 run 轮询（D7：2s）：只对当前展示的 queued/running run 拉详情；
  // 收敛（success/failed/cancelled）后停止并刷新历史（状态如实回显）。
  // 轮询失败保留上一次已知状态，不打断后续轮询。
  const resultId = result?.id;
  const resultStatus = result?.status;
  useEffect(() => {
    if (resultId == null || resultStatus == null) return;
    if (!isActiveRun(resultStatus)) return;
    const timer = setInterval(() => {
      void (async () => {
        try {
          const fresh = await fetchPipelineRun(pipelineId, resultId);
          setResult(fresh);
          if (!isActiveRun(fresh.status)) void loadHistory();
        } catch {
          // 保留上一次已知状态：轮询继续，不伪造进度。
        }
      })();
    }, pollIntervalMs);
    return () => clearInterval(timer);
  }, [pipelineId, resultId, resultStatus, loadHistory, pollIntervalMs]);

  const run = async () => {
    if (running) return;
    setRunning(true);
    setRunError(null);
    try {
      const detail = await runPipeline(pipelineId);
      setResult(detail);
      // 历史回到第 1 页（本次 run 即最新一条）。
      if (page !== 1) setPage(1);
      else void loadHistory();
    } catch (err) {
      setRunError(
        apiErrorText(err, t("console.datasets.pipeline.workbench.runError")),
      );
    } finally {
      setRunning(false);
    }
  };

  const openRun = async (runId: number) => {
    setRunError(null);
    try {
      setResult(await fetchPipelineRun(pipelineId, runId));
    } catch (err) {
      setRunError(
        apiErrorText(err, t("console.datasets.pipeline.workbench.runError")),
      );
    }
  };

  // 取消（D5）：POST 运行级取消端点；成功后本地如实置 cancel_requested_at
  // （不伪造节点进度），轮询继续到收敛为 cancelled。
  const cancel = async (runId: number) => {
    if (cancelling) return;
    setCancelling(true);
    setRunError(null);
    try {
      const res = await cancelRun(runId);
      setResult((prev) =>
        prev && prev.id === res.id
          ? {
              ...prev,
              status: res.status,
              cancel_requested_at: res.cancel_requested_at,
            }
          : prev,
      );
      void loadHistory();
    } catch (err) {
      setRunError(
        apiErrorText(err, t("console.datasets.pipeline.workbench.cancelError")),
      );
    } finally {
      setCancelling(false);
    }
  };

  const pages = Math.max(1, Math.ceil((history?.total ?? 0) / HISTORY_PAGE_SIZE));

  return (
    <section data-testid="pipeline-run-panel" className="mt-6">
      <div className="flex flex-wrap items-center gap-3">
        <h3 className="text-base font-semibold">
          {t("console.datasets.pipeline.workbench.runSectionTitle")}
        </h3>
        <button
          type="button"
          data-testid="pipeline-run-button"
          disabled={running}
          onClick={() => void run()}
          className="rounded bg-slate-900 px-3 py-1.5 text-xs text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-300"
        >
          {running
            ? t("console.datasets.pipeline.workbench.runRunning")
            : t("console.datasets.pipeline.workbench.runNow")}
        </button>
      </div>

      {runError && (
        <p
          data-testid="pipeline-run-error"
          className="mt-2 text-xs text-red-600"
        >
          {runError}
        </p>
      )}

      {result && (
        <RunResult
          run={result}
          onOpenDataset={onOpenDataset}
          onCancel={(runId) => void cancel(runId)}
          cancelling={cancelling}
        />
      )}

      {/* 运行历史（run 一级；点击展开到上方结果面板看 step 明细）。 */}
      <div className="mt-4">
        <h4 className="text-xs font-medium text-slate-600">
          {t("console.datasets.pipeline.workbench.historyTitle")}
        </h4>
        {historyError && (
          <p
            data-testid="pipeline-history-error"
            className="mt-2 text-xs text-red-600"
          >
            {t("console.datasets.pipeline.workbench.historyError")}
          </p>
        )}
        {history == null && !historyError && (
          <p data-testid="pipeline-history-loading" className="mt-2 text-xs text-slate-400">
            {t("console.datasets.pipeline.workbench.loading")}
          </p>
        )}
        {history != null && history.items.length === 0 && (
          <p data-testid="pipeline-history-empty" className="mt-2 text-xs text-slate-400">
            {t("console.datasets.pipeline.workbench.historyEmpty")}
          </p>
        )}
        {history != null && history.items.length > 0 && (
          <>
            <ul data-testid="pipeline-history" className="mt-2 space-y-1.5">
              {history.items.map((entry) => (
                <li key={entry.id}>
                  <button
                    type="button"
                    data-testid="pipeline-history-row"
                    data-run-id={entry.id}
                    data-status={entry.status}
                    data-trigger={entry.trigger}
                    onClick={() => void openRun(entry.id)}
                    className="flex w-full flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-slate-200 bg-white px-3 py-2 text-left text-xs hover:border-slate-400"
                  >
                    <RunStatusChip status={entry.status} />
                    <span
                      data-testid="pipeline-history-version"
                      className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-600"
                    >
                      v{entry.version}
                    </span>
                    <span
                      data-testid="pipeline-history-trigger"
                      data-trigger={entry.trigger}
                      className="rounded bg-sky-50 px-1.5 py-0.5 text-[11px] text-sky-700"
                    >
                      {triggerLabel(t, entry.trigger)}
                    </span>
                    <span data-testid="pipeline-history-time" className="text-slate-500">
                      {formatShortServerDate(entry.started_at)}
                    </span>
                    {entry.error_text && (
                      <span className="truncate font-mono text-[11px] text-red-500">
                        {entry.error_text}
                      </span>
                    )}
                  </button>
                </li>
              ))}
            </ul>
            {history.total > HISTORY_PAGE_SIZE && (
              <div
                data-testid="pipeline-history-pagination"
                className="mt-2 flex flex-wrap items-center gap-3 text-xs text-slate-500"
              >
                <span data-testid="pipeline-history-info">
                  {t("console.datasets.pipeline.workbench.historyPageInfo", {
                    total: history.total,
                    page,
                    pages,
                  })}
                </span>
                <button
                  type="button"
                  data-testid="pipeline-history-prev"
                  disabled={page <= 1}
                  onClick={() => setPage((current) => Math.max(1, current - 1))}
                  className="rounded border border-slate-300 px-2 py-1 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {t("console.datasets.pipeline.workbench.prevPage")}
                </button>
                <button
                  type="button"
                  data-testid="pipeline-history-next"
                  disabled={page >= pages}
                  onClick={() => setPage((current) => current + 1)}
                  className="rounded border border-slate-300 px-2 py-1 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {t("console.datasets.pipeline.workbench.nextPage")}
                </button>
              </div>
            )}
          </>
        )}
      </div>
    </section>
  );
}
