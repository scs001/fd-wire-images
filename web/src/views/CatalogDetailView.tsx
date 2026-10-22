// 指标详情页（#/catalog/:id，不登录可访问）：全部目录展示字段。
// 分级呈现：verified 给「读取数据」引导登录；registered 明确标注
// 「按需开通」并导流到需求单占位路由——页面不展示也不请求任何观测数值。
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  fetchIndicator,
  demandNewHref,
  CatalogApiError,
  type IndicatorDetail,
} from "../catalog/api";
import { DomainBadge, StatusBadge } from "../components/IndicatorBadges";
import { isLegalIndicator, LEX_PLATFORM_URL } from "../catalog/legal";

type DetailState = {
  loading: boolean;
  error: CatalogApiError | null;
  data: IndicatorDetail | null;
};

function formatSyncedAt(iso: string, language: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const locale = language === "zh" ? "zh-CN" : "en-US";
  try {
    return date.toLocaleString(locale, {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[10rem_1fr] gap-3 border-b border-slate-100 py-3 last:border-0 sm:grid-cols-[12rem_1fr]">
      <dt className="text-sm text-slate-500">{label}</dt>
      <dd className="text-sm text-slate-900 break-words">{children}</dd>
    </div>
  );
}

export function CatalogDetailView({ id }: { id: string }) {
  const { t, i18n } = useTranslation();
  const [state, setState] = useState<DetailState>({
    loading: true,
    error: null,
    data: null,
  });
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    let alive = true;
    setState((s) => ({ ...s, loading: true, error: null }));
    fetchIndicator(id)
      .then((data) => {
        if (alive) setState({ loading: false, error: null, data });
      })
      .catch((err: unknown) => {
        if (!alive) return;
        const error =
          err instanceof CatalogApiError ? err : new CatalogApiError(String(err));
        setState({ loading: false, error, data: null });
      });
    return () => {
      alive = false;
    };
  }, [id, reloadToken]);

  let body: React.ReactNode;
  if (state.loading && !state.data) {
    body = (
      <p className="py-12 text-center text-sm text-slate-400" data-testid="detail-loading">
        {t("catalog.loading")}
      </p>
    );
  } else if (state.error) {
    const notFound = state.error.status === 404;
    body = (
      <div
        className={`mt-6 rounded-lg border p-8 text-center ${
          notFound
            ? "border-slate-200"
            : "border-red-200 bg-red-50"
        }`}
        data-testid={notFound ? "detail-not-found" : "detail-error"}
      >
        <p
          className={`font-medium ${notFound ? "text-slate-700" : "text-red-700"}`}
        >
          {notFound
            ? t("catalog.detail.notFoundTitle")
            : t("catalog.errorTitle")}
        </p>
        <p
          className={`mt-1 text-sm ${notFound ? "text-slate-500" : "text-red-600"}`}
        >
          {notFound
            ? t("catalog.detail.notFoundHint")
            : t("catalog.errorHint")}
        </p>
        {!notFound && (
          <button
            type="button"
            onClick={() => setReloadToken((n) => n + 1)}
            className="mt-4 rounded-md border border-red-300 px-4 py-1.5 text-sm text-red-700 hover:bg-red-100 transition-colors"
          >
            {t("catalog.retry")}
          </button>
        )}
      </div>
    );
  } else if (state.data) {
    const d = state.data;
    const registered = d.status === "registered";
    // 法律域转介（wire-lex-referral）：条目保留可见，开通动线归识律——
    // 法律域数据不由 wire 供给，任何 status 一律转介（避免"绿色可读"假象）。
    const legal = isLegalIndicator(d);
    const timeSpan =
      d.time_span_start || d.time_span_end
        ? t("catalog.detail.timeSpanRange", {
            start: d.time_span_start || "…",
            end: d.time_span_end || "…",
          })
        : "—";
    body = (
      <div>
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-2xl font-semibold tracking-tight">
            {d.name_zh?.trim() || d.name_en?.trim() || d.code}
          </h1>
          <DomainBadge domain={d.domain} />
          <StatusBadge status={d.status} />
        </div>
        <p className="mt-1 font-mono text-sm text-slate-500">{d.code}</p>

        <dl className="mt-6" data-testid="detail-fields">
          <Field label={t("catalog.detail.nameZh")}>{d.name_zh?.trim() || "—"}</Field>
          <Field label={t("catalog.detail.nameEn")}>{d.name_en?.trim() || "—"}</Field>
          <Field label={t("catalog.detail.descriptionZh")}>
            {d.description_zh?.trim() || "—"}
          </Field>
          <Field label={t("catalog.detail.descriptionEn")}>
            {d.description_en?.trim() || "—"}
          </Field>
          <Field label={t("catalog.detail.domain")}>
            {d.domain?.trim() ? <DomainBadge domain={d.domain} /> : "—"}
          </Field>
          <Field label={t("catalog.detail.frequency")}>{d.frequency?.trim() || "—"}</Field>
          <Field label={t("catalog.detail.timeSpan")}>{timeSpan}</Field>
          <Field label={t("catalog.detail.entityCoverage")}>
            {d.entity_coverage_count != null
              ? t("catalog.detail.entities", { n: d.entity_coverage_count })
              : "—"}
          </Field>
          <Field label={t("catalog.detail.sampleValue")}>
            {d.sample_value != null && d.sample_value.trim() ? (
              <code className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-xs">
                {d.sample_value}
              </code>
            ) : (
              "—"
            )}
          </Field>
          <Field label={t("catalog.detail.status")}>
            <StatusBadge status={d.status} />
          </Field>
          <Field label={t("catalog.detail.syncedAt")}>
            <time dateTime={d.synced_at}>{formatSyncedAt(d.synced_at, i18n.language)}</time>
          </Field>
        </dl>

        {legal ? (
          <div
            className="mt-8 rounded-lg border border-indigo-200 bg-indigo-50 p-5"
            data-testid="detail-lex-referral"
          >
            <p className="text-sm text-indigo-900">{t("catalog.detail.lexReferralNote")}</p>
            <a
              href={LEX_PLATFORM_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="mt-4 inline-block rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 transition-colors"
            >
              {t("catalog.detail.lexReferralCta")}
            </a>
            <p className="mt-2 text-xs text-indigo-700">{t("catalog.detail.lexReferralHint")}</p>
          </div>
        ) : registered ? (
          <div
            className="mt-8 rounded-lg border border-amber-300 bg-amber-50 p-5"
            data-testid="detail-on-demand"
          >
            <p className="text-sm text-amber-800">{t("catalog.detail.registeredBanner")}</p>
            <a
              href={demandNewHref(d.demand_prefill?.indicator_code || d.code)}
              className="mt-4 inline-block rounded-md bg-amber-600 px-4 py-2 text-sm font-medium text-white hover:bg-amber-500 transition-colors"
            >
              {t("catalog.detail.enableOnDemand")}
            </a>
          </div>
        ) : (
          <div
            className="mt-8 rounded-lg border border-emerald-200 bg-emerald-50 p-5"
            data-testid="detail-direct-read"
          >
            <p className="text-sm text-emerald-800">{t("catalog.detail.verifiedNote")}</p>
            <a
              href="#/login"
              className="mt-4 inline-block rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 transition-colors"
            >
              {t("catalog.detail.readData")}
            </a>
            <p className="mt-2 text-xs text-emerald-700">{t("catalog.detail.readDataHint")}</p>
          </div>
        )}
      </div>
    );
  } else {
    body = null;
  }

  return (
    <div>
      <a
        href="#/catalog"
        className="text-sm text-slate-500 hover:text-slate-900 hover:underline"
      >
        ← {t("catalog.detail.back")}
      </a>
      <div className="mt-4">{body}</div>
    </div>
  );
}
