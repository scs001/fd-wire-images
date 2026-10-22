// 用量分轨概览页（#/admin/usage）：客户轨 / 平台轨数字卡 + 来源分布
// （壹座 yizuo / 萬星 wanxing / 平台内部 platform-internal / customer），
// 可按轨道与时间窗过滤——AI 探索（平台轨）不计费，两轨分开呈现。
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { AdminErrorBox, AdminStates } from "./AdminLayout";
import {
  fetchUsageOverview,
  type AggRow,
  type UsageOverview,
} from "./api";

const SOURCE_LABEL_KEYS: Record<string, string> = {
  yizuo: "admin.usage.sources.yizuo",
  wanxing: "admin.usage.sources.wanxing",
  "platform-internal": "admin.usage.sources.platformInternal",
  customer: "admin.usage.sources.customer",
};

const TRACK_CARD_CLASS: Record<string, string> = {
  customer: "border-sky-200 bg-sky-50/60",
  platform: "border-violet-200 bg-violet-50/60",
};

export function UsageOverviewPage() {
  const { t } = useTranslation();
  const [overview, setOverview] = useState<UsageOverview | null>(null);
  const [error, setError] = useState<{ status?: number; message: string; detail?: string } | null>(null);
  const [track, setTrack] = useState<"" | "customer" | "platform">("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");

  const load = useCallback(async () => {
    setError(null);
    setOverview(null);
    try {
      const data = await fetchUsageOverview({
        ...(track ? { credential_track: track } : {}),
        ...(start.trim() ? { start: start.trim() } : {}),
        ...(end.trim() ? { end: end.trim() } : {}),
      });
      setOverview(data);
    } catch (err) {
      setError(err as { status?: number; message: string });
    }
  }, [track, start, end]);

  useEffect(() => {
    void load();
  }, [load]);

  const trackByKey = new Map<string, AggRow>(
    (overview?.tracks ?? []).map((row) => [row.key, row]),
  );
  const zero = { key: "x", calls: 0, rows: 0 };

  return (
    <div>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold">{t("admin.usage.title")}</h1>
          <p className="mt-1 max-w-2xl text-xs text-slate-500">
            {t("admin.usage.subtitle")}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <label htmlFor="usage-track" className="text-xs text-slate-500">
            {t("admin.usage.trackFilter")}
          </label>
          <select
            id="usage-track"
            data-testid="usage-track-filter"
            value={track}
            onChange={(e) => setTrack(e.target.value as "" | "customer" | "platform")}
            className="rounded-md border border-slate-300 px-2 py-1 text-sm"
          >
            <option value="">{t("admin.usage.trackAll")}</option>
            <option value="customer">{t("admin.usage.trackCustomer")}</option>
            <option value="platform">{t("admin.usage.trackPlatform")}</option>
          </select>
          <input
            data-testid="usage-start"
            value={start}
            onChange={(e) => setStart(e.target.value)}
            placeholder={t("admin.usage.startPlaceholder")}
            className="w-40 rounded-md border border-slate-300 px-2 py-1 font-mono text-xs"
          />
          <input
            data-testid="usage-end"
            value={end}
            onChange={(e) => setEnd(e.target.value)}
            placeholder={t("admin.usage.endPlaceholder")}
            className="w-40 rounded-md border border-slate-300 px-2 py-1 font-mono text-xs"
          />
        </div>
      </div>

      {error && <AdminErrorBox error={error} onRetry={() => void load()} />}

      <AdminStates
        loading={overview == null && !error}
        empty={overview != null && overview.totals.calls === 0}
        emptyTestId="admin-usage-empty"
      >
        {overview && (
          <>
            <div className="mt-4 grid gap-3 sm:grid-cols-2" data-testid="admin-usage-cards">
              <TrackCard
                title={t("admin.usage.trackCustomer")}
                hint={t("admin.usage.trackCustomerHint")}
                data={trackByKey.get("customer") ?? zero}
                testId="admin-usage-card-customer"
                className={TRACK_CARD_CLASS.customer}
              />
              <TrackCard
                title={t("admin.usage.trackPlatform")}
                hint={t("admin.usage.trackPlatformHint")}
                data={trackByKey.get("platform") ?? zero}
                testId="admin-usage-card-platform"
                className={TRACK_CARD_CLASS.platform}
              />
            </div>

            <div className="mt-6">
              <h2 className="text-sm font-semibold">
                {t("admin.usage.bySourceTitle")}
              </h2>
              <table
                data-testid="admin-usage-source-table"
                className="mt-2 w-full border-collapse text-sm"
              >
                <thead>
                  <tr className="border-b border-slate-200 text-left text-xs text-slate-400">
                    <th className="py-2 pr-4">{t("admin.usage.colSource")}</th>
                    <th className="py-2 pr-4 text-right">{t("admin.usage.colCalls")}</th>
                    <th className="py-2 pr-4 text-right">{t("admin.usage.colRows")}</th>
                    <th className="py-2 pr-4 text-right">{t("admin.usage.colRowsShare")}</th>
                  </tr>
                </thead>
                <tbody>
                  {overview.by_source.map((row) => (
                    <tr
                      key={row.key}
                      data-testid="admin-usage-source-row"
                      className="border-b border-slate-100"
                    >
                      <td className="py-2 pr-4">
                        {t(SOURCE_LABEL_KEYS[row.key] ?? "", row.key)}
                      </td>
                      <td className="py-2 pr-4 text-right font-mono text-xs">
                        {row.calls.toLocaleString()}
                      </td>
                      <td className="py-2 pr-4 text-right font-mono text-xs">
                        {row.rows.toLocaleString()}
                      </td>
                      <td className="py-2 pr-4 text-right font-mono text-xs text-slate-500">
                        {overview.totals.rows > 0
                          ? `${((row.rows / overview.totals.rows) * 100).toFixed(1)}%`
                          : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </AdminStates>
    </div>
  );
}

function TrackCard({
  title,
  hint,
  data,
  testId,
  className,
}: {
  title: string;
  hint: string;
  data: AggRow;
  testId: string;
  className: string;
}) {
  const { t } = useTranslation();
  return (
    <div
      data-testid={testId}
      className={`rounded-lg border p-4 ${className ?? "border-slate-200 bg-white"}`}
    >
      <div className="text-xs font-medium text-slate-500">{title}</div>
      <div className="mt-2 flex items-baseline gap-2">
        <span
          data-testid={`${testId}-rows`}
          className="text-2xl font-semibold text-slate-900"
        >
          {data.rows.toLocaleString()}
        </span>
        <span className="text-xs text-slate-500">{t("admin.usage.rowsUnit")}</span>
      </div>
      <div className="mt-1 text-[11px] text-slate-500">
        {data.calls.toLocaleString()} {t("admin.usage.callsUnit")} · {hint}
      </div>
    </div>
  );
}
