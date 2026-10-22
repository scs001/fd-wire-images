// 管线调度小节（wire-transform-pipelines 任务 5.4）：管线级 cron 挂接
// （PUT {cron}）+ 暂停/恢复/删除；五字段 cron、naive UTC 口径诚实注记
// （与数据集「定时重派生」同款交互）。已挂接时详情头部同步显示
// 「输出已由管线调度管理」（由父组件按 schedule 态渲染）。
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  apiErrorText,
  deletePipelineSchedule,
  getPipelineSchedule,
  pausePipelineSchedule,
  putPipelineSchedule,
  resumePipelineSchedule,
  type PipelineScheduleInfo,
} from "../api";
import { formatServerDate } from "../util";

export function PipelineScheduleSection({
  pipelineId,
  onScheduleChanged,
}: {
  pipelineId: number;
  /** 调度态变化时回调（父组件更新「输出已由管线调度管理」提示）。 */
  onScheduleChanged: (schedule: PipelineScheduleInfo | null) => void;
}) {
  const { t } = useTranslation();
  // undefined = 加载中；null = 未挂接（登记面）；对象 = 已挂接态。
  const [schedule, setSchedule] = useState<PipelineScheduleInfo | null | undefined>(
    undefined,
  );
  const [loadError, setLoadError] = useState(false);
  const [cron, setCron] = useState("");
  const [busy, setBusy] = useState(false);
  const [opError, setOpError] = useState<string | null>(null);

  const apply = (next: PipelineScheduleInfo | null) => {
    setSchedule(next);
    setCron(next?.cron ?? "");
    onScheduleChanged(next);
  };

  const load = async () => {
    setLoadError(false);
    try {
      const next = await getPipelineSchedule(pipelineId);
      apply(next && next.cron ? next : null);
    } catch {
      setSchedule(undefined);
      setLoadError(true);
    }
  };

  useEffect(() => {
    setSchedule(undefined);
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pipelineId]);

  const save = async () => {
    if (busy || cron.trim() === "") return;
    setBusy(true);
    setOpError(null);
    try {
      apply(await putPipelineSchedule(pipelineId, cron.trim()));
    } catch (err) {
      // 服务端 detail（cron 非法五字段 / viewer 403 等）如实呈现。
      setOpError(
        apiErrorText(err, t("console.datasets.pipeline.workbench.scheduleError")),
      );
    } finally {
      setBusy(false);
    }
  };

  const toggle = async (action: "pause" | "resume") => {
    if (busy) return;
    setBusy(true);
    setOpError(null);
    try {
      const next =
        action === "pause"
          ? await pausePipelineSchedule(pipelineId)
          : await resumePipelineSchedule(pipelineId);
      apply(next);
    } catch (err) {
      setOpError(
        apiErrorText(err, t("console.datasets.pipeline.workbench.scheduleError")),
      );
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (busy) return;
    setBusy(true);
    setOpError(null);
    try {
      await deletePipelineSchedule(pipelineId);
      apply(null);
    } catch (err) {
      setOpError(
        apiErrorText(err, t("console.datasets.pipeline.workbench.scheduleError")),
      );
    } finally {
      setBusy(false);
    }
  };

  const registered = schedule != null;

  return (
    <section data-testid="pipeline-schedule-section" className="mt-6">
      <h3 className="text-base font-semibold">
        {t("console.datasets.pipeline.workbench.scheduleTitle")}
      </h3>
      <p className="mt-1 max-w-2xl text-xs text-slate-500">
        {t("console.datasets.pipeline.workbench.scheduleHint")}
      </p>
      {/* naive UTC 诚实注记（五字段 cron；客户时区换算属登记侧 v1 口径）。 */}
      <p
        data-testid="pipeline-schedule-utc-note"
        className="mt-1 text-xs text-amber-700"
      >
        {t("console.datasets.pipeline.workbench.scheduleUtcNote")}
      </p>

      {opError && (
        <p
          data-testid="pipeline-schedule-error"
          className="mt-2 text-xs text-red-600"
        >
          {opError}
        </p>
      )}

      {loadError && (
        <div data-testid="pipeline-schedule-load-error" className="mt-2">
          <p className="text-xs text-red-600">
            {t("console.datasets.pipeline.workbench.scheduleLoadError")}
          </p>
          <button
            type="button"
            data-testid="pipeline-schedule-retry"
            onClick={() => void load()}
            className="mt-1 rounded border border-red-300 px-2 py-0.5 text-xs text-red-700 hover:bg-red-100"
          >
            {t("console.datasets.pipeline.retry")}
          </button>
        </div>
      )}

      {schedule === undefined && !loadError && (
        <p
          data-testid="pipeline-schedule-loading"
          className="mt-2 text-xs text-slate-400"
        >
          {t("console.datasets.pipeline.workbench.loading")}
        </p>
      )}

      {schedule !== undefined && !loadError && (
        <div
          data-testid="pipeline-schedule-form"
          className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border border-slate-200 bg-white px-3 py-2.5 text-xs"
        >
          <label className="flex items-center gap-1.5 text-slate-600">
            <span className="text-slate-500">
              {t("console.datasets.pipeline.workbench.cronLabel")}
            </span>
            <input
              data-testid="pipeline-cron-input"
              value={cron}
              disabled={busy}
              onChange={(e) => setCron(e.target.value)}
              placeholder={t(
                "console.datasets.pipeline.workbench.cronPlaceholder",
              )}
              className="w-40 rounded border border-slate-300 px-2 py-1 font-mono text-xs"
            />
          </label>
          <button
            type="button"
            data-testid="pipeline-schedule-save"
            disabled={busy || cron.trim() === ""}
            onClick={() => void save()}
            className="rounded bg-slate-900 px-3 py-1 text-xs text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-300"
          >
            {busy
              ? t("console.datasets.pipeline.workbench.scheduleSaving")
              : registered
                ? t("console.datasets.pipeline.workbench.scheduleUpdate")
                : t("console.datasets.pipeline.workbench.scheduleSave")}
          </button>
        </div>
      )}

      {registered && schedule != null && (
        <div
          data-testid="pipeline-schedule-state"
          data-enabled={schedule.enabled ? "true" : "false"}
          data-cron={schedule.cron}
          className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 rounded border border-slate-200 bg-white px-3 py-2 text-xs"
        >
          <span
            data-testid="pipeline-schedule-enabled"
            data-enabled={schedule.enabled ? "true" : "false"}
            className={`rounded px-1.5 py-0.5 text-[11px] ${
              schedule.enabled
                ? "bg-emerald-50 text-emerald-700"
                : "bg-slate-100 text-slate-600"
            }`}
          >
            {schedule.enabled
              ? t("console.datasets.pipeline.workbench.scheduleRegistered")
              : t("console.datasets.pipeline.workbench.schedulePaused")}
          </span>
          <span
            data-testid="pipeline-schedule-cron"
            className="font-mono text-[11px] text-slate-700"
          >
            {schedule.cron}
          </span>
          <span data-testid="pipeline-schedule-last-run" className="text-slate-600">
            <span className="text-slate-400">
              {t("console.datasets.pipeline.workbench.scheduleLastRunLabel")}：
            </span>
            {schedule.last_run_at
              ? formatServerDate(schedule.last_run_at)
              : t("console.datasets.pipeline.workbench.scheduleNever")}
          </span>
          {schedule.last_status != null && (
            <span
              data-testid="pipeline-schedule-last-status"
              data-status={schedule.last_status}
              className={`rounded px-1.5 py-0.5 text-[11px] ${
                schedule.last_status === "success"
                  ? "bg-emerald-50 text-emerald-700"
                  : "bg-red-50 text-red-700"
              }`}
            >
              {schedule.last_status}
            </span>
          )}
          <span className="ml-auto flex items-center gap-1.5">
            {schedule.enabled ? (
              <button
                type="button"
                data-testid="pipeline-schedule-pause"
                disabled={busy}
                onClick={() => void toggle("pause")}
                className="rounded border border-slate-300 px-2 py-0.5 text-[11px] text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {t("console.datasets.pipeline.workbench.schedulePause")}
              </button>
            ) : (
              <button
                type="button"
                data-testid="pipeline-schedule-resume"
                disabled={busy}
                onClick={() => void toggle("resume")}
                className="rounded border border-emerald-200 px-2 py-0.5 text-[11px] text-emerald-700 hover:bg-emerald-50 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {t("console.datasets.pipeline.workbench.scheduleResume")}
              </button>
            )}
            <button
              type="button"
              data-testid="pipeline-schedule-delete"
              disabled={busy}
              onClick={() => void remove()}
              className="rounded border border-rose-200 px-2 py-0.5 text-[11px] text-rose-600 hover:bg-rose-50 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {t("console.datasets.pipeline.workbench.scheduleDelete")}
            </button>
          </span>
        </div>
      )}
    </section>
  );
}
