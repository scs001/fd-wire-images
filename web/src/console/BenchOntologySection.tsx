// 标准本体只读区（add-bench-wire-seam-contract 任务 2.3/2.4）：实体页顶部独立
// 区块，显示知验台（共享层）发布、本平台拉取的本体快照**元数据**。
//
// 为什么只出元数据：快照正文等于把共享层发布物免费复制进浏览器，而用户要回答
// 的问题是「拉到哪一版、规模多大、什么时候拉的」——三项都在元数据里。正文可
// 见性是另一件事，端点刻意不提供（design D3）。
//
// 三态各自如实，绝不合并：
// - 空数组 = 「还没拉过」（正常空态，不是错误、不是 403）；
// - 403 = ACL 受限，显示可读报因而不是空态（否则用户会以为确实没拉过）；
// - 其它错误 = 报因卡 + 重试。
//
// 与「版本」区的关系：版本区管 wire 自己的本地本体（可编辑、可回滚）；本区是
// 平台发布的共享本体（只读、单向）。两块分开显示是刻意的——合并会让人以为
// 拉下来的东西可以在本地改。
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  ConsoleApiError,
  apiErrorText,
  fetchBenchOntologyOverview,
  type BenchOntologySnapshotMeta,
} from "./api";

export function BenchOntologySection() {
  const { t } = useTranslation();
  const [domains, setDomains] = useState<BenchOntologySnapshotMeta[] | null>(
    null,
  );
  const [loadError, setLoadError] = useState<{
    text: string;
    status: number | null;
  } | null>(null);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      const overview = await fetchBenchOntologyOverview();
      setDomains(overview.domains);
    } catch (err) {
      setDomains(null);
      setLoadError({
        text: apiErrorText(err, t("console.entities.benchOntology.loadFailed")),
        status:
          err instanceof ConsoleApiError && typeof err.status === "number"
            ? err.status
            : null,
      });
    }
  }, [t]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <section
      data-testid="bench-ontology-section"
      className="rounded-lg border border-slate-200 bg-white p-4"
    >
      <div className="flex items-baseline justify-between gap-3">
        <h2 className="text-sm font-semibold text-slate-800">
          {t("console.entities.benchOntology.title")}
        </h2>
      </div>
      <p className="mt-1 text-xs text-slate-500">
        {t("console.entities.benchOntology.subtitle")}
      </p>

      {loadError && (
        <div
          data-testid="bench-ontology-error"
          className="mt-3 rounded-md border border-red-200 bg-red-50 p-3"
        >
          <p className="text-sm text-red-700">
            {loadError.status === 403
              ? t("console.entities.benchOntology.aclDenied")
              : loadError.text}
          </p>
          {loadError.status !== null && loadError.status !== 403 && (
            <p className="mt-1 font-mono text-[11px] text-red-500">
              HTTP {loadError.status}
            </p>
          )}
          <button
            type="button"
            data-testid="bench-ontology-retry"
            onClick={() => void load()}
            className="mt-2 rounded border border-red-300 px-2 py-0.5 text-xs text-red-700 hover:bg-red-100"
          >
            {t("console.entities.version.retry")}
          </button>
        </div>
      )}

      {domains === null && !loadError && (
        <div className="mt-4 min-h-[72px]" data-testid="bench-ontology-loading">
          <p className="text-xs text-slate-400">
            {t("console.entities.benchOntology.loading")}
          </p>
        </div>
      )}

      {domains != null && domains.length === 0 && !loadError && (
        <div
          data-testid="bench-ontology-empty"
          className="mt-3 rounded-md border border-dashed border-slate-300 p-4 text-center"
        >
          <p className="text-xs text-slate-500">
            {t("console.entities.benchOntology.empty")}
          </p>
        </div>
      )}

      {domains != null && domains.length > 0 && !loadError && (
        <ul className="mt-3 divide-y divide-slate-100">
          {domains.map((d) => (
            <li
              key={d.domain_key}
              data-testid={`bench-ontology-row-${d.domain_key}`}
              className="flex flex-wrap items-baseline gap-x-4 gap-y-1 py-2 text-xs text-slate-600"
            >
              <span className="font-mono text-slate-800">{d.domain_key}</span>
              <span>
                {t("console.entities.benchOntology.versionLabel")}{" "}
                <span className="font-mono">v{d.version}</span>
              </span>
              <span>
                {t("console.entities.benchOntology.digestLabel")}{" "}
                <span className="font-mono" title={d.digest}>
                  {d.digest.slice(0, 8)}
                </span>
              </span>
              <span>
                {t("console.entities.benchOntology.entitiesLabel")}{" "}
                <span className="font-mono">{d.entity_count}</span>
              </span>
              <span>
                {t("console.entities.benchOntology.relationshipsLabel")}{" "}
                <span className="font-mono">{d.relationship_count}</span>
              </span>
              <span className="ml-auto text-slate-400">
                {t("console.entities.benchOntology.pulledAtLabel")}{" "}
                <span className="font-mono">{d.pulled_at}</span>
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
