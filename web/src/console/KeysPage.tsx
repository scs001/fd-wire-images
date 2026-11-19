// 控制台「数据面 key」页（#/console/keys，wire-customer-data-engine 6.2）。
// - 成员可铸可吊（Q18）；明文仅铸造响应一次（与 wgk- 长命 key 同款纪律）。
// - allowlist 非空 = 仅列出的数据集；空 = 本企业全部。吊销即时生效。
// - key 永不进浏览器存储：明文只渲染一次供抄存，刷新即不再可见。
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  apiErrorText,
  createDataPlaneKey,
  fetchDataPlaneKeys,
  fetchDatasets,
  revokeDataPlaneKey,
  type DataPlaneKey,
  type DataPlaneKeyCreated,
  type Dataset,
} from "./api";

type Phase =
  | { kind: "idle" }
  | { kind: "minting" }
  | { kind: "minted"; created: DataPlaneKeyCreated };

export function KeysPage() {
  const { t } = useTranslation();
  const [keys, setKeys] = useState<DataPlaneKey[] | null>(null);
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  const [label, setLabel] = useState("");
  const [allowlist, setAllowlist] = useState<string[]>([]);
  const [copied, setCopied] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [keyList, datasetList] = await Promise.all([
        fetchDataPlaneKeys(),
        fetchDatasets(),
      ]);
      setKeys(keyList);
      setDatasets(datasetList);
    } catch (err) {
      setKeys(null);
      setError(apiErrorText(err, t("console.keys.errorTitle")));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void load();
  }, [load]);

  const mint = async () => {
    if (!label.trim()) return;
    setPhase({ kind: "minting" });
    try {
      const created = await createDataPlaneKey({
        label: label.trim(),
        dataset_ids: allowlist,
      });
      setPhase({ kind: "minted", created });
      setLabel("");
      setAllowlist([]);
      setCopied(false);
      void load();
    } catch (err) {
      setPhase({ kind: "idle" });
      setError(apiErrorText(err, t("console.keys.errorTitle")));
    }
  };

  const revoke = async (keyId: string) => {
    try {
      await revokeDataPlaneKey(keyId);
      void load();
    } catch (err) {
      setError(apiErrorText(err, t("console.keys.errorTitle")));
    }
  };

  const datasetName = (id: string): string => {
    const ds = datasets.find((d) => d.id === id);
    return ds ? ds.name : id.slice(0, 8);
  };

  return (
    <div data-testid="keys-page">
      <h1 className="text-lg font-semibold">{t("console.keys.title")}</h1>
      <p className="mt-1 max-w-2xl text-xs text-slate-500">
        {t("console.keys.subtitle")}
      </p>

      {phase.kind === "minted" && (
        <div
          data-testid="keys-once"
          className="mt-4 rounded-lg border border-emerald-200 bg-emerald-50/60 p-4"
        >
          <p className="text-sm font-semibold text-emerald-800">
            {t("console.keys.onceTitle")}
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <code className="break-all rounded bg-white px-2 py-1 font-mono text-xs text-slate-800">
              {phase.created.key}
            </code>
            <button
              type="button"
              data-testid="keys-once-copy"
              onClick={() => {
                void navigator.clipboard?.writeText(phase.created.key);
                setCopied(true);
              }}
              className="rounded border border-emerald-300 bg-white px-2 py-1 text-xs text-emerald-700 hover:bg-emerald-100"
            >
              {copied
                ? t("console.keys.copied")
                : t("console.keys.copy")}
            </button>
            <button
              type="button"
              data-testid="keys-once-dismiss"
              onClick={() => setPhase({ kind: "idle" })}
              className="rounded border border-emerald-300 bg-white px-2 py-1 text-xs text-emerald-700 hover:bg-emerald-100"
            >
              {t("console.keys.onceDismiss")}
            </button>
          </div>
          <p className="mt-2 text-xs text-emerald-700">
            {t("console.keys.onceHint")}
          </p>
        </div>
      )}

      <div className="mt-4 rounded-lg border border-slate-200 p-4">
        <p className="text-sm font-medium">{t("console.keys.mintTitle")}</p>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <input
            data-testid="keys-mint-label"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder={t("console.keys.labelPlaceholder")}
            maxLength={200}
            className="w-64 rounded-md border border-slate-300 px-2.5 py-1.5 text-sm focus:border-slate-500 focus:outline-none"
          />
          <button
            type="button"
            data-testid="keys-mint-submit"
            disabled={!label.trim() || phase.kind === "minting"}
            onClick={() => void mint()}
            className="rounded-md bg-slate-900 px-3 py-1.5 text-xs text-white hover:bg-slate-700 disabled:opacity-40"
          >
            {phase.kind === "minting"
              ? t("console.keys.minting")
              : t("console.keys.mintCta")}
          </button>
        </div>
        <p className="mt-3 text-xs text-slate-500">
          {t("console.keys.allowlistLabel")}
        </p>
        <div
          data-testid="keys-allowlist"
          className="mt-1.5 flex max-w-2xl flex-wrap gap-1.5"
        >
          {datasets.length === 0 && (
            <span className="text-xs text-slate-400">
              {t("console.keys.noDatasets")}
            </span>
          )}
          {datasets.map((ds) => {
            const on = allowlist.includes(ds.id);
            return (
              <button
                key={ds.id}
                type="button"
                data-testid={`keys-allow-${ds.key}`}
                onClick={() =>
                  setAllowlist((prev) =>
                    on ? prev.filter((x) => x !== ds.id) : [...prev, ds.id],
                  )
                }
                className={`rounded-full border px-2.5 py-0.5 text-xs transition-colors ${
                  on
                    ? "border-slate-900 bg-slate-900 text-white"
                    : "border-slate-300 text-slate-600 hover:bg-slate-100"
                }`}
              >
                {ds.name}
              </button>
            );
          })}
        </div>
        {allowlist.length > 0 && (
          <p className="mt-1.5 text-xs text-slate-400">
            {t("console.keys.allowlistScoped", { count: allowlist.length })}
          </p>
        )}
      </div>

      {error && (
        <div
          data-testid="keys-error"
          className="mt-4 rounded-md border border-red-200 bg-red-50 p-3"
        >
          <p className="break-all font-mono text-[11px] text-red-600">{error}</p>
        </div>
      )}

      {loading ? (
        <p className="mt-6 text-xs text-slate-400" data-testid="keys-loading">
          {t("console.keys.loading")}
        </p>
      ) : (
        <div className="mt-4 overflow-x-auto">
          <table
            data-testid="keys-table"
            className="w-full min-w-[40rem] border-collapse text-sm"
          >
            <thead>
              <tr className="border-b border-slate-200 text-left text-xs text-slate-400">
                <th className="py-2 pr-4 font-medium">
                  {t("console.keys.colLabel")}
                </th>
                <th className="py-2 pr-4 font-medium">
                  {t("console.keys.colFingerprint")}
                </th>
                <th className="py-2 pr-4 font-medium">
                  {t("console.keys.colScope")}
                </th>
                <th className="py-2 pr-4 font-medium">
                  {t("console.keys.colStatus")}
                </th>
                <th className="py-2 pr-4 font-medium">
                  {t("console.keys.colLastUsed")}
                </th>
                <th className="py-2 font-medium" />
              </tr>
            </thead>
            <tbody>
              {(keys ?? []).map((k) => (
                <tr
                  key={k.id}
                  data-testid={`keys-row-${k.fingerprint}`}
                  className="border-b border-slate-100"
                >
                  <td className="py-2 pr-4 text-slate-800">{k.label}</td>
                  <td className="py-2 pr-4 font-mono text-xs text-slate-600">
                    wdk-{k.fingerprint.slice(0, 8)}…
                  </td>
                  <td className="py-2 pr-4 text-xs text-slate-600">
                    {k.dataset_ids.length === 0
                      ? t("console.keys.scopeAll")
                      : k.dataset_ids.map(datasetName).join("、")}
                  </td>
                  <td className="py-2 pr-4">
                    <span
                      className={`rounded px-1.5 py-0.5 text-xs ${
                        k.status === "active"
                          ? "bg-emerald-50 text-emerald-700"
                          : "bg-slate-100 text-slate-400"
                      }`}
                    >
                      {k.status === "active"
                        ? t("console.keys.statusActive")
                        : t("console.keys.statusRevoked")}
                    </span>
                  </td>
                  <td className="py-2 pr-4 font-mono text-xs text-slate-500">
                    {k.last_used_at
                      ? k.last_used_at.slice(0, 16).replace("T", " ")
                      : "—"}
                  </td>
                  <td className="py-2 text-right">
                    {k.status === "active" && (
                      <button
                        type="button"
                        data-testid={`keys-revoke-${k.fingerprint}`}
                        onClick={() => void revoke(k.id)}
                        className="rounded border border-red-200 px-2 py-0.5 text-xs text-red-600 hover:bg-red-50"
                      >
                        {t("console.keys.revokeCta")}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
              {(keys ?? []).length === 0 && (
                <tr>
                  <td
                    colSpan={6}
                    data-testid="keys-empty"
                    className="py-6 text-center text-xs text-slate-400"
                  >
                    {t("console.keys.empty")}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      <p className="mt-6 max-w-2xl text-xs text-slate-400">
        {t("console.keys.disciplineNote")}
      </p>
    </div>
  );
}
