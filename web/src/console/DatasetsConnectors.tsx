// 控制台「我的数据」页的连接器管理区（wire-customer-local-data 任务组 3）。
// 三块：配对向导（一次性令牌 + 安装指引）/ 连接器状态列表（在线离线·心跳·吊销）/
// 白名单与调度（选连接器 → 圈定源 → 命名 → cron → 建 connector 类数据集）。
// 免费档（wire-free-tier-data-gate 4.2）：区块可见但标注付费——连接器建集经
// 引擎物化属付费面，免费档不渲染向导/列表/白名单（不发起建集），点击付费徽标
// 展开升档说明。
// 契约：openspec/changes/wire-customer-local-data/reports/interfaces.md §4。
// wire-connector-distribution 任务 3：安装指引接下载清单（按平台真实链接 +
// 版本/sha256 展示与复制），状态列表加「最新版本」对照（心跳版本落后即提示）。
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  apiErrorText,
  connectorDownloadUrl,
  createDataset,
  createPairingToken,
  fetchConnectorManifest,
  fetchConnectors,
  revokeConnector,
  type Connector,
  type ConnectorManifest,
  type PairingToken,
} from "./api";
import { formatServerDate } from "./util";
import { CONTACT_MAILTO } from "../views/HomeView";

export function ConnectorsSection({
  planCode,
  onCreated,
}: {
  /** 客户档位（wire-free-tier-data-gate 4.2）：free=区块可见但标注付费。 */
  planCode?: string;
  onCreated: () => void;
}) {
  const { t } = useTranslation();
  const free = planCode === "free";
  const [paidNoteOpen, setPaidNoteOpen] = useState(false);
  const [connectors, setConnectors] = useState<Connector[] | null>(null);
  const [listError, setListError] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [token, setToken] = useState<PairingToken | null>(null);
  const [generating, setGenerating] = useState(false);
  const [pairError, setPairError] = useState<string | null>(null);
  const [revokingId, setRevokingId] = useState<string | null>(null);
  // 下载清单（公开端点）：manifest=null 且 unavailable=false 为加载中；
  // 404/网络错 → unavailable=true——向导如实显示「暂不可下载」，不隐藏步骤。
  const [manifest, setManifest] = useState<ConnectorManifest | null>(null);
  const [manifestUnavailable, setManifestUnavailable] = useState(false);

  const load = useCallback(async () => {
    setListError(false);
    try {
      setConnectors(await fetchConnectors());
    } catch {
      setConnectors([]);
      setListError(true);
    }
  }, []);

  useEffect(() => {
    // 免费档不发起任何连接器请求（连接器建集经引擎，属付费面）。
    if (free) return;
    setConnectors(null);
    void load();
  }, [free, load]);

  useEffect(() => {
    // 下载清单为公开端点，但同样只在付费态取（免费档不渲染消费它的区块）。
    if (free) return;
    let cancelled = false;
    fetchConnectorManifest()
      .then((data) => {
        if (!cancelled) setManifest(data);
      })
      .catch(() => {
        if (!cancelled) setManifestUnavailable(true);
      });
    return () => {
      cancelled = true;
    };
  }, [free]);

  const generate = async () => {
    setGenerating(true);
    setPairError(null);
    try {
      setToken(await createPairingToken());
    } catch (err) {
      setPairError(
        apiErrorText(err, t("console.datasets.connectors.pairing.errorTitle")),
      );
    } finally {
      setGenerating(false);
    }
  };

  const revoke = async (connectorId: string) => {
    setRevokingId(connectorId);
    setActionError(null);
    try {
      await revokeConnector(connectorId);
      await load();
    } catch (err) {
      setActionError(
        apiErrorText(err, t("console.datasets.connectors.list.revokeError")),
      );
    } finally {
      setRevokingId(null);
    }
  };

  const active = (connectors ?? []).filter((c) => !c.revoked_at);

  return (
    <section data-testid="connectors-section" className="mt-8">
      <h2 className="text-base font-semibold">
        {t("console.datasets.connectors.title")}
        {free && (
          <button
            type="button"
            data-testid="connectors-paid-badge"
            onClick={() => setPaidNoteOpen((open) => !open)}
            className="ml-2 align-middle rounded-full bg-amber-50 px-2 py-0.5 text-xs font-normal text-amber-700 ring-1 ring-amber-200 hover:bg-amber-100"
          >
            {t("console.datasets.paidFeature.badge")}
          </button>
        )}
      </h2>
      <p className="mt-1 max-w-2xl text-xs text-slate-500">
        {t("console.datasets.connectors.subtitle")}
      </p>

      {free && paidNoteOpen && (
        <div
          data-testid="connectors-paid-note"
          className="mt-2 max-w-2xl rounded-md border border-amber-200 bg-amber-50/60 p-3"
        >
          <p className="text-sm text-amber-700">
            {t("console.datasets.paidFeature.connectorsTitle")}
          </p>
          <p className="mt-1 text-xs text-amber-700">
            {t("console.datasets.paidFeature.connectorsBody")}
          </p>
          <a
            data-testid="connectors-paid-cta"
            href={CONTACT_MAILTO}
            className="mt-2 inline-block rounded-md bg-slate-900 px-3 py-1.5 text-xs text-white hover:bg-slate-700"
          >
            {t("console.datasets.quota.cta")}
          </a>
        </div>
      )}

      {free ? (
        <p
          data-testid="connectors-paid-body"
          className="mt-1 max-w-2xl text-xs text-slate-400"
        >
          {t("console.datasets.paidFeature.connectorsShort")}
        </p>
      ) : (
        <>
          <div
            data-testid="pairing-wizard"
            className="mt-4 rounded-lg border border-slate-200 bg-white p-4"
          >
            <h3 className="text-sm font-semibold">
              {t("console.datasets.connectors.pairing.title")}
            </h3>
            <p className="mt-1 text-xs text-slate-500">
              {t("console.datasets.connectors.pairing.hint")}
            </p>
            <button
              type="button"
              data-testid="pairing-generate"
              disabled={generating}
              onClick={() => void generate()}
              className="mt-2 rounded-md bg-slate-900 px-3 py-1.5 text-xs text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-300"
            >
              {generating
                ? t("console.datasets.connectors.pairing.generating")
                : t("console.datasets.connectors.pairing.generate")}
            </button>
            {pairError && (
              <p data-testid="pairing-error" className="mt-2 text-xs text-red-600">
                {pairError}
              </p>
            )}
            {token && (
              <PairingTokenPanel
                key={token.token}
                token={token}
                manifest={manifest}
                manifestUnavailable={manifestUnavailable}
                onDismiss={() => setToken(null)}
              />
            )}
          </div>

          <div
            data-testid="connector-list-section"
            className="mt-4 rounded-lg border border-slate-200 bg-white p-4"
          >
            <h3 className="text-sm font-semibold">
              {t("console.datasets.connectors.list.title")}
            </h3>
            {connectors == null && !listError && (
              <p data-testid="connectors-loading" className="mt-2 text-xs text-slate-400">
                {t("console.datasets.loading")}
              </p>
            )}
            {listError && (
              <div data-testid="connectors-error" className="mt-2">
                <p className="text-xs text-red-600">
                  {t("console.datasets.connectors.list.errorTitle")}
                </p>
                <button
                  type="button"
                  data-testid="connectors-retry"
                  onClick={() => void load()}
                  className="mt-1 rounded border border-red-300 px-2 py-0.5 text-xs text-red-700 hover:bg-red-100"
                >
                  {t("console.datasets.retry")}
                </button>
              </div>
            )}
            {actionError && (
              <p data-testid="connectors-action-error" className="mt-2 text-xs text-red-600">
                {actionError}
              </p>
            )}
            {connectors != null && connectors.length === 0 && !listError && (
              <p data-testid="connectors-empty" className="mt-2 text-xs text-slate-400">
                {t("console.datasets.connectors.list.empty")}
              </p>
            )}
            {connectors != null && connectors.length > 0 && (
              <ul data-testid="connector-list" className="mt-2 space-y-2">
                {connectors.map((connector) => {
                  const revoked = connector.revoked_at != null;
                  const status = revoked ? "revoked" : connector.status;
                  // 最新版本对照（任务 3.2）：manifest 在场且心跳版本与之不同
                  // 即提示升级（manifest 即发布通道，sha- 版本以一致为准绳；
                  // 心跳未上报版本或清单不可用时只呈现，不比较）。
                  const latest = manifest?.version ?? null;
                  const outdated =
                    manifest != null &&
                    connector.version != null &&
                    connector.version !== manifest.version;
                  return (
                    <li
                      key={connector.id}
                      data-testid="connector-item"
                      data-status={status}
                      className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded border border-slate-100 bg-slate-50/60 px-3 py-2"
                    >
                      <span
                        data-testid="connector-name"
                        className="text-sm text-slate-800"
                      >
                        {connector.name}
                      </span>
                      <span
                        data-testid="connector-status"
                        data-status={status}
                        className={`rounded-full px-2 py-0.5 text-[11px] ring-1 ${
                          status === "online"
                            ? "bg-emerald-50 text-emerald-700 ring-emerald-200"
                            : status === "revoked"
                              ? "bg-rose-50 text-rose-700 ring-rose-200"
                              : "bg-slate-100 text-slate-600 ring-slate-200"
                        }`}
                      >
                        {t(`console.datasets.connectors.list.${status}`)}
                      </span>
                      <span data-testid="connector-version" className="text-[11px] text-slate-500">
                        {t("console.datasets.connectors.list.version")}{" "}
                        {connector.version ??
                          t("console.datasets.connectors.list.unknown")}
                      </span>
                      <span
                        data-testid="connector-latest"
                        data-outdated={outdated ? "true" : "false"}
                        className="text-[11px] text-slate-500"
                      >
                        {t("console.datasets.connectors.list.latest")}{" "}
                        {latest ?? t("console.datasets.connectors.list.unknown")}
                        {outdated && (
                          <span
                            data-testid="connector-upgrade-hint"
                            className="ml-1 rounded-full bg-amber-50 px-1.5 py-0.5 text-amber-700 ring-1 ring-amber-200"
                          >
                            {t("console.datasets.connectors.list.upgradeHint", {
                              version: latest ?? "",
                            })}
                          </span>
                        )}
                      </span>
                      <span data-testid="connector-heartbeat" className="text-[11px] text-slate-500">
                        {t("console.datasets.connectors.list.heartbeat")}{" "}
                        {connector.last_heartbeat_at
                          ? formatServerDate(connector.last_heartbeat_at)
                          : t("console.datasets.connectors.list.unknown")}
                      </span>
                      {!revoked && (
                        <button
                          type="button"
                          data-testid="connector-revoke"
                          disabled={revokingId === connector.id}
                          onClick={() => void revoke(connector.id)}
                          className="ml-auto rounded border border-rose-300 px-2 py-1 text-xs text-rose-700 hover:bg-rose-50 disabled:cursor-not-allowed disabled:text-slate-400"
                        >
                          {revokingId === connector.id
                            ? t("console.datasets.connectors.list.revoking")
                            : t("console.datasets.connectors.list.revoke")}
                        </button>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
          </div>

          <WhitelistForm
            connectors={active}
            loading={connectors == null}
            onCreated={onCreated}
          />
        </>
      )}
    </section>
  );
}

function PairingTokenPanel({
  token,
  manifest,
  manifestUnavailable,
  onDismiss,
}: {
  token: PairingToken;
  /** 下载清单（null 且 unavailable=false 为加载中；不可用时如实降级）。 */
  manifest: ConnectorManifest | null;
  manifestUnavailable: boolean;
  onDismiss: () => void;
}) {
  const { t } = useTranslation();
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const handle = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(handle);
  }, []);

  const remainingMs = Math.max(0, new Date(token.expires_at).getTime() - now);
  const expired = remainingMs <= 0;
  const minutes = String(Math.floor(remainingMs / 60000)).padStart(2, "0");
  const seconds = String(Math.floor((remainingMs % 60000) / 1000)).padStart(2, "0");
  const apiBase = typeof window !== "undefined" ? window.location.origin : "";

  return (
    <div
      data-testid="pairing-token-panel"
      className="mt-3 rounded-md border border-emerald-200 bg-emerald-50/50 p-3"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-slate-500">
          {t("console.datasets.connectors.pairing.tokenLabel")}
        </span>
        <code
          data-testid="pairing-token"
          className="break-all rounded bg-white px-2 py-0.5 font-mono text-xs text-slate-800"
        >
          {token.token}
        </code>
      </div>
      <p data-testid="pairing-once-hint" className="mt-1.5 text-[11px] text-amber-700">
        {t("console.datasets.connectors.pairing.onceHint")}
      </p>
      <p
        data-testid="pairing-countdown"
        className={`mt-1 text-[11px] ${expired ? "text-red-600" : "text-slate-500"}`}
      >
        {expired
          ? t("console.datasets.connectors.pairing.expired")
          : t("console.datasets.connectors.pairing.countdown", {
              time: `${minutes}:${seconds}`,
            })}
      </p>
      <h4 className="mt-3 text-xs font-semibold">
        {t("console.datasets.connectors.pairing.installTitle")}
      </h4>
      {/* 第一步：真实下载链接（manifest 在场时按平台列出，含版本与 sha256）；
          不可用时如实「暂不可下载」并保留下方命令行指引（不隐藏步骤）。 */}
      {manifestUnavailable ? (
        <p
          data-testid="connector-downloads-unavailable"
          className="mt-1 text-[11px] text-amber-700"
        >
          {t("console.datasets.connectors.pairing.downloadUnavailable")}
        </p>
      ) : manifest == null ? (
        <p
          data-testid="connector-downloads-loading"
          className="mt-1 text-[11px] text-slate-400"
        >
          {t("console.datasets.connectors.pairing.manifestLoading")}
        </p>
      ) : (
        <ConnectorDownloads manifest={manifest} apiBase={apiBase} />
      )}
      <p className="mt-1 text-[11px] text-slate-600">
        {t("console.datasets.connectors.pairing.step2")}
      </p>
      <pre
        data-testid="pairing-command"
        className="mt-1.5 overflow-x-auto rounded bg-white p-2 font-mono text-[11px] text-slate-700"
      >
        {t("console.datasets.connectors.pairing.pairCommand", {
          token: token.token,
          api: apiBase,
        })}
      </pre>
      <a
        data-testid="pairing-docs-link"
        href="#/docs?section=local-data"
        className="mt-1.5 inline-block text-[11px] text-slate-500 underline hover:text-slate-700"
      >
        {t("console.datasets.connectors.pairing.docsLink")}
      </a>
      <button
        type="button"
        data-testid="pairing-dismiss"
        onClick={onDismiss}
        className="mt-2 rounded border border-slate-300 bg-white px-2 py-1 text-xs text-slate-600 hover:bg-slate-100"
      >
        {t("console.datasets.connectors.pairing.dismiss")}
      </button>
    </div>
  );
}

// 下载清单按平台落地为真实链接（任务 3.1）：版本随清单头呈现，每平台
// 链接 + 文件名 + sha256（截断展示、复制按钮复制全文）；available=false
// 的平台如实标注缺失，不渲染链接。
function ConnectorDownloads({
  manifest,
  apiBase,
}: {
  manifest: ConnectorManifest;
  apiBase: string;
}) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState<string | null>(null);

  const copySha = async (platform: string, sha256: string) => {
    try {
      await navigator.clipboard?.writeText(sha256);
      setCopied(platform);
      window.setTimeout(
        () => setCopied((cur) => (cur === platform ? null : cur)),
        2000,
      );
    } catch {
      /* 剪贴板不可用（非安全上下文等）：无反馈即可，不阻塞下载流程 */
    }
  };

  return (
    <div data-testid="connector-downloads" data-version={manifest.version}>
      <p className="mt-1 text-[11px] font-medium text-slate-600">
        {t("console.datasets.connectors.pairing.downloadsTitle", {
          version: manifest.version,
        })}
      </p>
      <ul className="mt-1 space-y-1">
        {manifest.platforms.map((p) => (
          <li
            key={p.platform}
            data-testid="connector-download-item"
            data-platform={p.platform}
            data-available={p.available ? "true" : "false"}
            className="flex flex-wrap items-center gap-x-2 gap-y-0.5"
          >
            {p.available ? (
              <a
                data-testid="connector-download-link"
                href={connectorDownloadUrl(apiBase, p.platform)}
                className="rounded bg-white px-1.5 py-0.5 font-mono text-[11px] text-emerald-700 underline ring-1 ring-emerald-200 hover:bg-emerald-50"
              >
                {p.platform}
              </a>
            ) : (
              <span
                data-testid="connector-download-missing"
                className="rounded bg-white px-1.5 py-0.5 font-mono text-[11px] text-slate-400 ring-1 ring-slate-200"
              >
                {p.platform}
              </span>
            )}
            <span className="font-mono text-[10px] text-slate-400">
              {p.filename}
            </span>
            {p.available ? (
              <>
                <code
                  data-testid="connector-download-sha"
                  className="font-mono text-[10px] text-slate-500"
                >
                  {p.sha256.slice(0, 16)}…
                </code>
                <button
                  type="button"
                  data-testid="connector-download-copy"
                  onClick={() => void copySha(p.platform, p.sha256)}
                  className="rounded border border-slate-300 bg-white px-1.5 py-0.5 text-[10px] text-slate-600 hover:bg-slate-100"
                >
                  {copied === p.platform
                    ? t("console.datasets.connectors.pairing.copied")
                    : t("console.datasets.connectors.pairing.copySha")}
                </button>
              </>
            ) : (
              <span className="text-[10px] text-amber-700">
                {t("console.datasets.connectors.pairing.platformMissing")}
              </span>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

function WhitelistForm({
  connectors,
  loading,
  onCreated,
}: {
  connectors: Connector[];
  loading: boolean;
  onCreated: () => void;
}) {
  const { t } = useTranslation();
  const [connectorId, setConnectorId] = useState("");
  const [sourceName, setSourceName] = useState("");
  const [sourceRef, setSourceRef] = useState("");
  const [name, setName] = useState("");
  const [cron, setCron] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState(false);

  // 源名与源内表/文件名都必填：任一为空即禁用提交（前端校验）。
  const canSubmit =
    connectorId !== "" &&
    sourceName.trim() !== "" &&
    sourceRef.trim() !== "" &&
    name.trim() !== "";

  const submit = async () => {
    if (!canSubmit || submitting) return;
    setSubmitting(true);
    setError(null);
    setCreated(false);
    try {
      await createDataset({
        name: name.trim(),
        source: {
          kind: "connector",
          connector_id: connectorId,
          source_name: sourceName.trim(),
          source_ref: sourceRef.trim(),
          schedule_cron: cron.trim() === "" ? null : cron.trim(),
        },
      });
      setConnectorId("");
      setSourceName("");
      setSourceRef("");
      setName("");
      setCron("");
      setCreated(true);
      onCreated();
    } catch (err) {
      setError(
        apiErrorText(
          err,
          t("console.datasets.connectors.whitelist.errorTitle"),
        ),
      );
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      data-testid="whitelist-section"
      className="mt-4 rounded-lg border border-slate-200 bg-white p-4"
    >
      <h3 className="text-sm font-semibold">
        {t("console.datasets.connectors.whitelist.title")}
      </h3>
      <p className="mt-1 text-xs text-slate-500">
        {t("console.datasets.connectors.whitelist.hint")}
      </p>
      {!loading && connectors.length === 0 && (
        <p data-testid="whitelist-empty" className="mt-2 text-xs text-slate-400">
          {t("console.datasets.connectors.whitelist.empty")}
        </p>
      )}
      {(loading || connectors.length > 0) && (
        <form
          className="mt-3 flex flex-wrap items-start gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          <div>
            <label htmlFor="whitelist-connector" className="block text-xs text-slate-500">
              {t("console.datasets.connectors.whitelist.connectorLabel")}
            </label>
            <select
              id="whitelist-connector"
              data-testid="whitelist-connector"
              value={connectorId}
              onChange={(e) => setConnectorId(e.target.value)}
              className="mt-1 w-44 rounded-md border border-slate-300 px-2 py-1 text-sm"
            >
              <option value="">
                {t("console.datasets.connectors.whitelist.connectorPlaceholder")}
              </option>
              {connectors.map((connector) => (
                <option key={connector.id} value={connector.id}>
                  {connector.name}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="whitelist-source-name" className="block text-xs text-slate-500">
              {t("console.datasets.connectors.whitelist.sourceNameLabel")}
            </label>
            <input
              id="whitelist-source-name"
              data-testid="whitelist-source-name"
              value={sourceName}
              onChange={(e) => setSourceName(e.target.value)}
              placeholder={t(
                "console.datasets.connectors.whitelist.sourceNamePlaceholder",
              )}
              className="mt-1 w-44 rounded-md border border-slate-300 px-2 py-1 text-sm"
            />
            <p className="mt-1 max-w-56 text-[11px] text-slate-400">
              {t("console.datasets.connectors.whitelist.sourceNameHint")}
            </p>
          </div>
          <div>
            <label htmlFor="whitelist-source-ref" className="block text-xs text-slate-500">
              {t("console.datasets.connectors.whitelist.sourceRefLabel")}
            </label>
            <input
              id="whitelist-source-ref"
              data-testid="whitelist-source-ref"
              value={sourceRef}
              onChange={(e) => setSourceRef(e.target.value)}
              placeholder={t(
                "console.datasets.connectors.whitelist.sourceRefPlaceholder",
              )}
              className="mt-1 w-44 rounded-md border border-slate-300 px-2 py-1 text-sm"
            />
            <p className="mt-1 max-w-56 text-[11px] text-slate-400">
              {t("console.datasets.connectors.whitelist.sourceRefHint")}
            </p>
          </div>
          <div>
            <label htmlFor="whitelist-name" className="block text-xs text-slate-500">
              {t("console.datasets.connectors.whitelist.nameLabel")}
            </label>
            <input
              id="whitelist-name"
              data-testid="whitelist-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={t(
                "console.datasets.connectors.whitelist.namePlaceholder",
              )}
              className="mt-1 w-44 rounded-md border border-slate-300 px-2 py-1 text-sm"
            />
          </div>
          <div>
            <label htmlFor="whitelist-cron" className="block text-xs text-slate-500">
              {t("console.datasets.connectors.whitelist.cronLabel")}
            </label>
            <input
              id="whitelist-cron"
              data-testid="whitelist-cron"
              value={cron}
              onChange={(e) => setCron(e.target.value)}
              placeholder={t(
                "console.datasets.connectors.whitelist.cronPlaceholder",
              )}
              className="mt-1 w-36 rounded-md border border-slate-300 px-2 py-1 font-mono text-sm"
            />
            <p className="mt-1 text-[11px] text-slate-400">
              {t("console.datasets.connectors.whitelist.cronHint")}
            </p>
          </div>
          <button
            type="submit"
            data-testid="whitelist-submit"
            disabled={!canSubmit || submitting}
            className="mt-5 rounded-md bg-slate-900 px-3 py-1.5 text-xs text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-300"
          >
            {submitting
              ? t("console.datasets.connectors.whitelist.submitting")
              : t("console.datasets.connectors.whitelist.submit")}
          </button>
        </form>
      )}
      <p className="mt-2 text-[11px] text-slate-400">
        {t("console.datasets.connectors.whitelist.requiredHint")}
      </p>
      {error && (
        <p data-testid="whitelist-error" className="mt-2 text-xs text-red-600">
          {error}
        </p>
      )}
      {created && (
        <p data-testid="whitelist-created" className="mt-2 text-xs text-emerald-600">
          {t("console.datasets.connectors.whitelist.created")}
        </p>
      )}
    </div>
  );
}