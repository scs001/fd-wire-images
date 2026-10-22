// 目录徽标：domain（中性）与 status（语义色）。
// verified = 绿 + ✓「可直读」；registered = 琥珀「按需开通」。
import { useTranslation } from "react-i18next";
import type { IndicatorStatus } from "../catalog/api";

export function StatusBadge({ status }: { status: IndicatorStatus }) {
  const { t } = useTranslation();

  if (status === "verified") {
    return (
      <span
        data-testid="status-badge"
        className="inline-flex items-center gap-1 rounded-full bg-emerald-50 px-2 py-0.5 text-xs font-medium text-emerald-700 ring-1 ring-emerald-600/20"
      >
        <svg
          aria-hidden="true"
          viewBox="0 0 16 16"
          fill="none"
          className="h-3 w-3"
          stroke="currentColor"
          strokeWidth="2"
        >
          <path d="M3 8.5 6.5 12 13 4.5" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        {t("catalog.statusVerified")}
      </span>
    );
  }
  return (
    <span
      data-testid="status-badge"
      className="inline-flex items-center gap-1 rounded-full bg-amber-50 px-2 py-0.5 text-xs font-medium text-amber-700 ring-1 ring-amber-600/20"
    >
      <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-amber-500" />
      {t("catalog.statusRegistered")}
    </span>
  );
}

export function DomainBadge({ domain }: { domain: string | null }) {
  if (!domain || !domain.trim()) return null;
  return (
    <span
      data-testid="domain-badge"
      className="inline-flex items-center rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-600 ring-1 ring-slate-500/10"
    >
      {domain.trim()}
    </span>
  );
}
