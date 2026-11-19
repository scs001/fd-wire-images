// 证据引用块（wire-analysis-console 4.7）：每条证据显示原文逐字片段、
// 发布日与来源，并可点开原文。高亮用浏览器原生 Text Fragment
// （#:~:text=），不注入脚本、不改目标页。
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { fetchArtifactEvidence, type ArtifactEvidence } from "./api";

function withTextFragment(url: string, quote: string): string {
  const base = url.split("#")[0];
  return `${base}#:~:text=${encodeURIComponent(quote)}`;
}

export function EvidenceList({ artifactId }: { artifactId: number }) {
  const { t } = useTranslation();
  const [items, setItems] = useState<ArtifactEvidence[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    fetchArtifactEvidence(artifactId)
      .then((rows) => {
        if (alive) setItems(rows);
      })
      .catch((err: Error) => {
        if (alive) setError(err.message);
      })
      .finally(() => {
        if (alive) setLoaded(true);
      });
    return () => {
      alive = false;
    };
  }, [artifactId]);

  if (!loaded || (items.length === 0 && error === null)) return null;

  return (
    <section
      data-testid="artifact-evidence"
      className="rounded-lg border border-slate-200 bg-white p-4"
    >
      <h3 className="mb-3 text-sm font-semibold text-slate-800">
        {t("evidence.title")}
      </h3>
      {error !== null && (
        <p className="text-xs text-rose-700">{error}</p>
      )}
      <ul className="space-y-3">
        {items.map((item) => (
          <li
            key={`${item.stable_id}-${item.index}`}
            data-testid="artifact-evidence-item"
            className="rounded-md bg-slate-50 p-3 text-sm"
          >
            {item.claim && (
              <div className="mb-1 font-medium text-slate-800">{item.claim}</div>
            )}
            <blockquote className="border-l-2 border-slate-300 pl-2 text-slate-700">
              {item.quote}
            </blockquote>
            <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-slate-500">
              <span>
                {item.source} · {t("evidence.asOf")} {item.as_of}
              </span>
              {item.quote_ok === false && (
                <span className="rounded bg-amber-100 px-1.5 py-0.5 text-amber-800">
                  {t("evidence.mismatch")}
                </span>
              )}
              {item.url ? (
                <a
                  className="text-sky-700 underline"
                  href={withTextFragment(item.url, item.quote)}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {t("evidence.open")}
                </a>
              ) : (
                <span>{t("evidence.unavailable")}</span>
              )}
              {item.error && item.quote_ok !== false && (
                <span className="text-slate-400">{item.error}</span>
              )}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
