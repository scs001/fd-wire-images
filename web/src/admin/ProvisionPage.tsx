// cell 配给状态页（#/admin/provisions，add-self-serve-cell-provision 任务
// 1.3）：升档触发的自动配给链观测面——请求列表（新→旧）+ 状态徽标 +
// 失败原因可见。红线（首期只读）：无任何操作按钮/写面——处置动作只有
// 「重试升档」（改档幂等复位 failed 行）与宿主侧 runner，运营页零写入口。
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { AdminErrorBox, AdminStates } from "./AdminLayout";
import { fetchProvisionRequests, type AdminProvisionRequest } from "./api";

/** 单页条数（与后端默认 50 同值；limit 上限 200 由后端收敛）。 */
const PAGE_SIZE = 50;

/** 状态徽标配色（排队=sky / 执行中=amber / 完成=emerald / 失败=rose）。 */
const STATE_BADGE_CLASS: Record<AdminProvisionRequest["state"], string> = {
  queued: "bg-sky-50 text-sky-700 ring-sky-200",
  running: "bg-amber-50 text-amber-700 ring-amber-200",
  done: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  failed: "bg-rose-50 text-rose-700 ring-rose-200",
};

export function ProvisionPage() {
  const { t } = useTranslation();
  const [items, setItems] = useState<AdminProvisionRequest[] | null>(null);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState<
    { status?: number; message: string; detail?: string } | null
  >(null);

  const load = useCallback(async () => {
    setError(null);
    setItems(null);
    try {
      const page = await fetchProvisionRequests({ limit: PAGE_SIZE, offset });
      setItems(page.items);
      setTotal(page.total);
    } catch (err) {
      setError(err as { status?: number; message: string });
    }
  }, [offset]);

  useEffect(() => {
    void load();
  }, [load]);

  const from = total === 0 ? 0 : offset + 1;
  const to = Math.min(offset + PAGE_SIZE, total);

  return (
    <div>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold">{t("admin.provisions.title")}</h1>
          <p className="mt-1 max-w-2xl text-xs text-slate-500">
            {t("admin.provisions.subtitle")}
          </p>
        </div>
        <span className="text-xs text-slate-400">
          {t("admin.provisions.pageInfo", { from, to, total })}
        </span>
      </div>

      {error && <AdminErrorBox error={error} onRetry={() => void load()} />}

      <AdminStates
        loading={items == null && !error}
        empty={(items?.length ?? 0) === 0 && !error}
        emptyTestId="admin-provisions-empty"
      >
        {items != null && items.length > 0 && (
          <table
            data-testid="admin-provisions-table"
            className="mt-4 w-full border-collapse text-sm"
          >
            <thead>
              <tr className="border-b border-slate-200 text-left text-xs text-slate-400">
                <th className="py-2 pr-4">#</th>
                <th className="py-2 pr-4">{t("admin.provisions.colCustomer")}</th>
                <th className="py-2 pr-4">{t("admin.provisions.colState")}</th>
                <th className="py-2 pr-4">{t("admin.provisions.colError")}</th>
                <th className="py-2 pr-4">{t("admin.provisions.colCreatedAt")}</th>
              </tr>
            </thead>
            <tbody>
              {items!.map((req) => (
                <tr
                  key={req.id}
                  data-testid="admin-provisions-row"
                  className="border-b border-slate-100"
                >
                  <td className="py-2 pr-4 text-xs text-slate-400">{req.id}</td>
                  <td className="py-2 pr-4">
                    <span
                      data-testid="admin-provisions-customer"
                      className="font-mono text-xs"
                    >
                      {req.customer_id}
                    </span>
                  </td>
                  <td className="py-2 pr-4">
                    <span
                      data-testid={`admin-provisions-state-${req.id}`}
                      className={`rounded-full px-2 py-0.5 text-xs ring-1 ${
                        STATE_BADGE_CLASS[req.state] ??
                        "bg-slate-100 text-slate-500 ring-slate-200"
                      }`}
                    >
                      {t(`admin.provisions.state.${req.state}`)}
                    </span>
                  </td>
                  <td className="max-w-[20rem] py-2 pr-4 text-xs text-rose-600">
                    <span
                      data-testid="admin-provisions-error"
                      className="line-clamp-2"
                      title={req.error ?? undefined}
                    >
                      {req.error ?? "—"}
                    </span>
                  </td>
                  <td className="py-2 pr-4 font-mono text-xs">
                    {req.created_at.replace("T", " ").slice(0, 16)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {items != null && items.length > 0 && (
          <p className="mt-2 text-[11px] text-slate-400">
            {t("admin.provisions.readonlyNote")}
          </p>
        )}
        {total > PAGE_SIZE && (
          <div className="mt-3 flex items-center gap-3">
            <button
              type="button"
              data-testid="admin-provisions-prev"
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              className="rounded-md border border-slate-300 px-3 py-1 text-xs text-slate-600 hover:bg-slate-100 disabled:opacity-40"
            >
              {t("admin.provisions.prev")}
            </button>
            <button
              type="button"
              data-testid="admin-provisions-next"
              disabled={to >= total}
              onClick={() => setOffset(offset + PAGE_SIZE)}
              className="rounded-md border border-slate-300 px-3 py-1 text-xs text-slate-600 hover:bg-slate-100 disabled:opacity-40"
            >
              {t("admin.provisions.next")}
            </button>
          </div>
        )}
      </AdminStates>
    </div>
  );
}
