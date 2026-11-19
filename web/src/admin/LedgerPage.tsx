// 账本页（#/admin/ledger）：按客户 / 周期过滤的流水表，kind 徽标语义着色。
// 只读视图——记账入口在结算管道（billing Ledger），本页不改账。
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { AdminErrorBox, AdminStates } from "./AdminLayout";
import {
  fetchCustomerUsage,
  fetchCustomers,
  fetchLedger,
  type AdminCustomer,
  type CustomerUsage,
  type LedgerEntry,
  type LedgerKind,
} from "./api";

const KIND_BADGE_CLASS: Record<LedgerKind, string> = {
  grant: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  overage: "bg-amber-50 text-amber-700 ring-amber-200",
  hardstop: "bg-rose-50 text-rose-700 ring-rose-200",
  demand: "bg-sky-50 text-sky-700 ring-sky-200",
  adjustment: "bg-violet-50 text-violet-700 ring-violet-200",
};

const ALL_KINDS = Object.keys(KIND_BADGE_CLASS) as LedgerKind[];

export function LedgerPage() {
  const { t } = useTranslation();
  const [customers, setCustomers] = useState<AdminCustomer[]>([]);
  const [entries, setEntries] = useState<LedgerEntry[] | null>(null);
  const [usage, setUsage] = useState<CustomerUsage | null>(null);
  const [error, setError] = useState<{ status?: number; message: string; detail?: string } | null>(null);
  const [customerId, setCustomerId] = useState<string>("");
  const [period, setPeriod] = useState("");

  const load = useCallback(async () => {
    setError(null);
    setEntries(null);
    setUsage(null);
    try {
      const customerList = await fetchCustomers();
      setCustomers(customerList);
      const id = customerId ? Number(customerId) : undefined;
      const [rows, usageData] = await Promise.all([
        fetchLedger({
          ...(id != null ? { customer_id: id } : {}),
          ...(period.trim() ? { period: period.trim() } : {}),
        }),
        id != null
          ? fetchCustomerUsage(id)
          : Promise.resolve(null),
      ]);
      setEntries(rows);
      setUsage(usageData);
    } catch (err) {
      setError(err as { status?: number; message: string });
    }
  }, [customerId, period]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold">{t("admin.ledger.title")}</h1>
          <p className="mt-1 max-w-2xl text-xs text-slate-500">
            {t("admin.ledger.subtitle")}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <label htmlFor="ledger-customer" className="text-xs text-slate-500">
            {t("admin.ledger.customerFilter")}
          </label>
          <select
            id="ledger-customer"
            data-testid="ledger-customer-filter"
            value={customerId}
            onChange={(e) => setCustomerId(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1 text-sm"
          >
            <option value="">{t("admin.ledger.allCustomers")}</option>
            {customers.map((customer) => (
              <option key={customer.id} value={String(customer.id)}>
                {customer.name}
              </option>
            ))}
          </select>
          <label htmlFor="ledger-period" className="text-xs text-slate-500">
            {t("admin.ledger.periodFilter")}
          </label>
          <input
            id="ledger-period"
            data-testid="ledger-period-filter"
            value={period}
            onChange={(e) => setPeriod(e.target.value)}
            placeholder="YYYY-MM"
            className="w-28 rounded-md border border-slate-300 px-2 py-1 font-mono text-xs"
          />
        </div>
      </div>

      {usage && (
        <div
          data-testid="ledger-usage-summary"
          className="mt-4 flex flex-wrap items-center gap-4 rounded-lg border border-slate-200 bg-slate-50/60 px-4 py-3 text-xs text-slate-600"
        >
          <span>
            {t("admin.ledger.usageOf", { name: usage.customer_name })}：
            <span data-testid="ledger-usage-rows" className="font-semibold text-slate-800">
              {usage.totals.rows.toLocaleString()}
            </span>
            {" "}
            {t("admin.ledger.rowsUnit")} · {usage.totals.calls.toLocaleString()}{" "}
            {t("admin.ledger.callsUnit")}
          </span>
          {usage.caller ? (
            <span className="font-mono text-[11px] text-slate-400">
              caller={usage.caller}
            </span>
          ) : (
            <span className="text-[11px] text-amber-600">
              {t("admin.ledger.noCallerMapping")}
            </span>
          )}
        </div>
      )}

      {error && <AdminErrorBox error={error} onRetry={() => void load()} />}

      <AdminStates
        loading={entries == null && !error}
        empty={(entries?.length ?? 0) === 0 && !error}
        emptyTestId="admin-ledger-empty"
      >
        {entries != null && entries.length > 0 && (
          <table
          data-testid="admin-ledger-table"
          className="mt-4 w-full border-collapse text-sm"
        >
          <thead>
            <tr className="border-b border-slate-200 text-left text-xs text-slate-400">
              <th className="py-2 pr-4">#</th>
              <th className="py-2 pr-4">{t("admin.ledger.colKind")}</th>
              <th className="py-2 pr-4">{t("admin.ledger.colPeriod")}</th>
              <th className="py-2 pr-4 text-right">{t("admin.ledger.colQuantity")}</th>
              <th className="py-2 pr-4 text-right">{t("admin.ledger.colAmount")}</th>
              <th className="py-2 pr-4">{t("admin.ledger.colRef")}</th>
            </tr>
          </thead>
          <tbody>
            {entries!.map((entry) => (
              <tr
                key={entry.id}
                data-testid="admin-ledger-row"
                className="border-b border-slate-100"
              >
                <td className="py-2 pr-4 text-xs text-slate-400">{entry.id}</td>
                <td className="py-2 pr-4">
                  <span
                    data-testid="admin-ledger-kind-badge"
                    data-kind={entry.kind}
                    className={`rounded-full px-2 py-0.5 text-xs ring-1 ${
                      KIND_BADGE_CLASS[entry.kind] ?? "bg-slate-100 text-slate-600 ring-slate-200"
                    }`}
                  >
                    {t(`admin.ledger.kinds.${entry.kind}`, entry.kind)}
                  </span>
                </td>
                <td className="py-2 pr-4 font-mono text-xs">{entry.period}</td>
                <td className="py-2 pr-4 text-right font-mono text-xs">
                  {entry.quantity.toLocaleString()}
                </td>
                <td className="py-2 pr-4 text-right font-mono text-xs">
                  <span data-testid="admin-ledger-amount">
                    {entry.amount_yuan_str}
                  </span>
                </td>
                <td className="py-2 pr-4 font-mono text-[11px] text-slate-400">
                  {entry.source_ref ?? entry.idempotency_key}
                </td>
              </tr>
            ))}
          </tbody>
          </table>
        )}
        {entries != null && entries.length > 0 && (
          <p className="mt-2 text-[11px] text-slate-400">
            {t("admin.ledger.kindLegend")}：
            {ALL_KINDS.map((kind) => t(`admin.ledger.kinds.${kind}`)).join(" / ")}
          </p>
        )}
      </AdminStates>
    </div>
  );
}
