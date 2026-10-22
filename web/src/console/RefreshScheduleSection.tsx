// 定时刷新小节（wire-refresh-scheduler 任务 4.1）：derived 数据集详情页
// （DatasetDetail）与看板查看态（DashboardPane）共用的管理小节——
// 无调度时显示创建面（间隔预设档，档位按目标类型从其间隔地板起步：
// dataset 1h/6h/24h、artifact 6h/24h/7d）；有调度时显示状态行
// （间隔/下次运行/上次结果/上次错误）+ 暂停恢复/删除按钮。
// 契约：GET/POST/DELETE /api/v1/refresh-schedules、POST /{id}/pause|resume
// （见 api.ts schedules 区块）；错误经 apiErrorText 如实呈现后端 detail。
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  apiErrorText,
  createRefreshSchedule,
  deleteRefreshSchedule,
  listRefreshSchedules,
  pauseRefreshSchedule,
  resumeRefreshSchedule,
  type RefreshSchedule,
  type RefreshScheduleTargetType,
} from "./api";
import { formatServerDate } from "./util";

/** dataset 目标预设档：1h/6h/24h（地板 60 分钟起步）。 */
export const DATASET_SCHEDULE_INTERVALS = [60, 360, 1440] as const;

/** artifact 目标预设档：6h/24h/7d（地板 360 分钟起步——AI 刷新耗 token）。 */
export const ARTIFACT_SCHEDULE_INTERVALS = [360, 1440, 10080] as const;

export function RefreshScheduleSection({
  targetType,
  targetId,
  intervals,
}: {
  targetType: RefreshScheduleTargetType;
  /** dataset 目标 = 数据集 id；artifact 目标 = 产物 id（字符串化）。 */
  targetId: string;
  /** 预设间隔档（分钟）；首档为创建面默认选中项。 */
  intervals: readonly number[];
}) {
  const { t } = useTranslation();
  // undefined = 加载中；null = 无调度（显示创建面）；对象 = 已有调度。
  const [schedule, setSchedule] = useState<RefreshSchedule | null | undefined>(
    undefined,
  );
  const [loadError, setLoadError] = useState(false);
  const [intervalMinutes, setIntervalMinutes] = useState<number>(intervals[0]!);
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState(false);
  const [opError, setOpError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoadError(false);
    try {
      const rows = await listRefreshSchedules();
      setSchedule(
        rows.find(
          (row) => row.target_type === targetType && row.target_id === targetId,
        ) ?? null,
      );
    } catch {
      setSchedule(undefined);
      setLoadError(true);
    }
  }, [targetType, targetId]);

  useEffect(() => {
    setSchedule(undefined);
    void load();
  }, [load]);

  const create = async () => {
    if (creating) return;
    setCreating(true);
    setOpError(null);
    try {
      setSchedule(
        await createRefreshSchedule({
          target_type: targetType,
          target_id: targetId,
          interval_minutes: intervalMinutes,
        }),
      );
    } catch (err) {
      // 服务端 detail（地板/类型/配额校验文案）如实呈现。
      setOpError(apiErrorText(err, t("console.schedules.opError")));
    } finally {
      setCreating(false);
    }
  };

  const togglePaused = async () => {
    if (schedule == null || busy) return;
    setBusy(true);
    setOpError(null);
    try {
      setSchedule(
        schedule.paused
          ? await resumeRefreshSchedule(schedule.id)
          : await pauseRefreshSchedule(schedule.id),
      );
    } catch (err) {
      setOpError(apiErrorText(err, t("console.schedules.opError")));
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (schedule == null || busy) return;
    setBusy(true);
    setOpError(null);
    try {
      await deleteRefreshSchedule(schedule.id);
      setSchedule(null);
    } catch (err) {
      setOpError(apiErrorText(err, t("console.schedules.opError")));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section
      data-testid="schedule-section"
      data-target-type={targetType}
      data-target-id={targetId}
      className="mt-6"
    >
      <h2 className="text-base font-semibold">{t("console.schedules.title")}</h2>
      <p className="mt-1 max-w-2xl text-xs text-slate-500">
        {t("console.schedules.hint")}
      </p>

      {opError && (
        <p data-testid="schedule-op-error" className="mt-2 text-xs text-red-600">
          {opError}
        </p>
      )}

      {loadError && (
        <div data-testid="schedule-load-error" className="mt-2">
          <p className="text-xs text-red-600">
            {t("console.schedules.loadError")}
          </p>
          <button
            type="button"
            data-testid="schedule-retry"
            onClick={() => void load()}
            className="mt-1 rounded border border-red-300 px-2 py-0.5 text-xs text-red-700 hover:bg-red-100"
          >
            {t("console.schedules.retry")}
          </button>
        </div>
      )}

      {schedule === undefined && !loadError && (
        <p data-testid="schedule-loading" className="mt-2 text-xs text-slate-400">
          {t("console.schedules.loading")}
        </p>
      )}

      {schedule === null && !loadError && (
        <div
          data-testid="schedule-create"
          className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border border-slate-200 bg-white px-3 py-2.5 text-xs"
        >
          <span className="text-slate-500">
            {t("console.schedules.intervalLabel")}
          </span>
          <div
            role="radiogroup"
            aria-label={t("console.schedules.intervalLabel")}
            className="flex flex-wrap gap-3"
          >
            {intervals.map((minutes) => (
              <label
                key={minutes}
                className="flex items-center gap-1 text-slate-700"
              >
                <input
                  type="radio"
                  data-testid="schedule-interval"
                  data-minutes={minutes}
                  name={`schedule-interval-${targetType}-${targetId}`}
                  checked={intervalMinutes === minutes}
                  onChange={() => setIntervalMinutes(minutes)}
                />
                {/* 预设档文案（console.schedules.intervals.*）；非预设值回落原文。 */}
                {t(`console.schedules.intervals.${minutes}`, `${minutes} min`)}
              </label>
            ))}
          </div>
          <button
            type="button"
            data-testid="schedule-create-submit"
            disabled={creating}
            onClick={() => void create()}
            className="ml-auto rounded bg-slate-900 px-3 py-1 text-xs text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-300"
          >
            {creating
              ? t("console.schedules.creating")
              : t("console.schedules.createSubmit")}
          </button>
        </div>
      )}

      {schedule != null && (
        <div
          data-testid="schedule-row"
          data-paused={schedule.paused ? "true" : "false"}
          className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 rounded border border-slate-200 bg-white px-3 py-2 text-xs"
        >
          <span
            data-testid="schedule-state-badge"
            data-paused={schedule.paused ? "true" : "false"}
            className={`rounded px-1.5 py-0.5 text-[11px] ${
              schedule.paused
                ? "bg-amber-50 text-amber-700"
                : "bg-emerald-50 text-emerald-700"
            }`}
          >
            {schedule.paused
              ? t("console.schedules.pausedBadge")
              : t("console.schedules.activeBadge")}
          </span>
          <span data-testid="schedule-interval-current">
            {t(
              `console.schedules.intervals.${schedule.interval_minutes}`,
              `${schedule.interval_minutes} min`,
            )}
          </span>
          <span data-testid="schedule-next-run" className="text-slate-600">
            <span className="text-slate-400">
              {t("console.schedules.nextRunLabel")}：
            </span>
            {formatServerDate(schedule.next_run_at)}
          </span>
          {schedule.last_status != null && (
            <span
              data-testid="schedule-last-status"
              data-status={schedule.last_status}
              className={`rounded px-1.5 py-0.5 text-[11px] ${
                schedule.last_status === "ok"
                  ? "bg-emerald-50 text-emerald-700"
                  : schedule.last_status === "failed"
                    ? "bg-red-50 text-red-700"
                    : "bg-slate-100 text-slate-600"
              }`}
            >
              {t("console.schedules.lastStatusLabel")}：
              {t(
                `console.schedules.statuses.${schedule.last_status}`,
                schedule.last_status,
              )}
            </span>
          )}
          {schedule.last_error && (
            <span
              data-testid="schedule-last-error"
              title={t("console.schedules.lastErrorLabel")}
              className="font-mono text-[11px] text-red-500"
            >
              {schedule.last_error}
            </span>
          )}
          <div className="ml-auto flex gap-2">
            <button
              type="button"
              data-testid="schedule-toggle"
              disabled={busy}
              onClick={() => void togglePaused()}
              className="rounded border border-slate-300 px-2 py-0.5 text-[11px] text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {schedule.paused
                ? t("console.schedules.resume")
                : t("console.schedules.pause")}
            </button>
            <button
              type="button"
              data-testid="schedule-delete"
              disabled={busy}
              onClick={() => void remove()}
              className="rounded border border-rose-200 px-2 py-0.5 text-[11px] text-rose-600 hover:bg-rose-50 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {t("console.schedules.delete")}
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
