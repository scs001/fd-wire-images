// 单记录页分享与二维码弹层（wire-record-view 任务 4.3，D5 纯前端 QR）。
//
// 打开即拉产物详情解析主数据集+键列 → 键值选择器（keyColumn 去重值下拉，
// 上限 500，或手输兜底）→ 生成分享链接（既有 POST /artifacts/{id}/share 通道
// 的 record-view 放行；ttl_days 白名单 0|7|30|90——0=不设期·印刷物档，
// expires_at 返回 null 如实呈现 + 撤销提示「归档即失效」）→ 展示完整公开页
// URL（origin + #/share/record-view/{id}?t=…&k=…）→ 二维码（qrcode 库浏览器
// 构建**仅在此弹层动态 import 懒加载**——公开页 bundle 零增量；下载 PNG
// （高清倍率）与 SVG 两种；逐键生成，不做批量打包）。
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  apiErrorText,
  buildRecordViewSharePageUrl,
  createRecordViewShareLink,
  fetchLifecycleArtifact,
  queryDatasetDistinct,
  RECORD_VIEW_TTL_DAYS,
  type RecordViewShareLink,
  type RecordViewTtlDays,
} from "../api";
import { formatServerDate } from "../util";
import {
  normalizeDistinctOptions,
} from "../form/formSpec";
import {
  parseRecordViewSpec,
  RECORD_VIEW_LIMITS,
} from "./recordViewSpec";

export function RecordViewShareDialog({
  artifactId,
  onClose,
}: {
  artifactId: number;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  // 产物 spec（主数据集 + 键列——键值选择器的查询面）。
  const [specInfo, setSpecInfo] = useState<{
    datasetId: string;
    keyColumn: string;
  } | null>(null);
  const [specError, setSpecError] = useState<string | null>(null);
  // 键值下拉（distinct ≤500）。
  const [keyOptions, setKeyOptions] = useState<string[] | null>(null);
  const [keyTruncated, setKeyTruncated] = useState(false);
  const [keyLoadError, setKeyLoadError] = useState<string | null>(null);
  // 分享生成（ttl 变更即重新生成——令牌随生成时刻钉死版本）。
  const [ttlDays, setTtlDays] = useState<RecordViewTtlDays>(30);
  const [result, setResult] = useState<RecordViewShareLink | null>(null);
  const [shareError, setShareError] = useState<string | null>(null);
  // 键值（下拉选择或手输兜底——手输优先）。
  const [selectedKey, setSelectedKey] = useState("");
  const [manualKey, setManualKey] = useState("");
  const [copied, setCopied] = useState(false);
  // 二维码（纯前端懒加载生成）。
  const [qrPng, setQrPng] = useState<string | null>(null);
  const [qrSvg, setQrSvg] = useState<string | null>(null);
  const [qrError, setQrError] = useState<string | null>(null);

  // 打开即拉产物详情：解析主数据集/键列（未绑定 → 如实提示不可分享）。
  useEffect(() => {
    let alive = true;
    setSpecError(null);
    fetchLifecycleArtifact(artifactId)
      .then((detail) => {
        if (!alive) return;
        const body = parseRecordViewSpec(detail.spec);
        if (
          body == null ||
          body.datasetId == null ||
          !body.keyColumn ||
          !body.keyColumn.trim()
        ) {
          setSpecError(t("console.recordview.share.unbound"));
          return;
        }
        setSpecInfo({ datasetId: body.datasetId, keyColumn: body.keyColumn });
      })
      .catch((err) => {
        if (alive) {
          setSpecError(
            apiErrorText(err, t("console.dashboard.share.generateFailed")),
          );
        }
      });
    return () => {
      alive = false;
    };
  }, [artifactId, t]);

  // 键值下拉：主数据集 keyColumn 去重值（≤500，升序去重）。
  useEffect(() => {
    let alive = true;
    if (specInfo == null) return;
    setKeyOptions(null);
    setKeyTruncated(false);
    setKeyLoadError(null);
    queryDatasetDistinct(specInfo.datasetId, {
      column: specInfo.keyColumn,
      limit: RECORD_VIEW_LIMITS.distinctKeys,
    })
      .then((distinct) => {
        if (!alive) return;
        const { options, truncated } = normalizeDistinctOptions(
          distinct?.values ?? [],
          RECORD_VIEW_LIMITS.distinctKeys,
          distinct?.truncated ?? false,
        );
        setKeyOptions(options);
        setKeyTruncated(truncated);
      })
      .catch((err) => {
        if (!alive) return;
        // 下拉加载失败不阻塞：手输兜底仍可用。
        setKeyLoadError(
          apiErrorText(err, t("console.recordview.share.keysLoadFailed")),
        );
      });
    return () => {
      alive = false;
    };
  }, [specInfo, t]);

  // 生成分享链接（打开即生成；ttl 改选重新生成——0 档 expires_at=null）。
  useEffect(() => {
    let alive = true;
    setResult(null);
    setShareError(null);
    createRecordViewShareLink(artifactId, ttlDays)
      .then((link) => {
        if (alive) setResult(link);
      })
      .catch((err) => {
        if (alive) {
          setShareError(
            apiErrorText(err, t("console.dashboard.share.generateFailed")),
          );
        }
      });
    return () => {
      alive = false;
    };
  }, [artifactId, ttlDays, t]);

  // t 从数据端点 url 的 query 解出（相对路径也能被 URL 解析出 search）。
  const token = useMemo(
    () =>
      result
        ? new URL(result.url, window.location.origin).searchParams.get("t") ?? ""
        : "",
    [result],
  );
  const effectiveKey = manualKey.trim() || selectedKey;
  // 完整公开页链接：origin + #/share/record-view/{id}?t=…&k=…（k 进 URL）。
  const shareUrl =
    result && token && effectiveKey
      ? buildRecordViewSharePageUrl(
          window.location.origin,
          artifactId,
          token,
          effectiveKey,
        )
      : "";

  // 二维码：qrcode 库浏览器构建**仅在此懒加载**（公开页 bundle 零增量）；
  // PNG 高清倍率（width 1024）与 SVG 双格式，URL 变化即重生成。
  useEffect(() => {
    let alive = true;
    if (!shareUrl) {
      setQrPng(null);
      setQrSvg(null);
      setQrError(null);
      return;
    }
    setQrPng(null);
    setQrSvg(null);
    setQrError(null);
    void import("qrcode")
      .then(async (QRCode) => {
        const [png, svg] = await Promise.all([
          QRCode.toDataURL(shareUrl, { width: 1024, margin: 2 }),
          QRCode.toString(shareUrl, { type: "svg", margin: 2 }),
        ]);
        if (!alive) return;
        setQrPng(png);
        setQrSvg(svg);
      })
      .catch(() => {
        if (!alive) return;
        setQrError(t("console.recordview.share.qrFailed"));
      });
    return () => {
      alive = false;
    };
  }, [shareUrl, t]);

  const copyLink = async () => {
    if (!shareUrl) return;
    try {
      await navigator.clipboard.writeText(shareUrl);
      setCopied(true);
    } catch {
      /* 剪贴板不可用（如非安全上下文）：如实不提示已复制 */
    }
  };

  /** SVG 下载：Blob URL + 程序化 anchor（文件名带键值，逐键生成）。 */
  const downloadSvg = () => {
    if (qrSvg == null || !effectiveKey) return;
    const blob = new Blob([qrSvg], { type: "image/svg+xml" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `record-view-${artifactId}-${effectiveKey}.svg`;
    document.body.appendChild(anchor);
    anchor.click();
    document.body.removeChild(anchor);
    URL.revokeObjectURL(url);
  };

  /** 到期展示：0 档（expires_at=null）呈「不设期（印刷物）」文案。 */
  const expiresText = result
    ? result.expires_at == null
      ? t("console.recordview.share.noExpiry")
      : formatServerDate(result.expires_at)
    : ttlDays === 0
      ? t("console.recordview.share.noExpiry")
      : formatServerDate(
          new Date(Date.now() + ttlDays * 86_400_000)
            .toISOString()
            .slice(0, 19),
        );

  return (
    <div
      data-testid="record-view-share-dialog"
      className="fixed inset-0 z-40 flex items-center justify-center p-4"
    >
      <button
        type="button"
        aria-label={t("console.dashboard.share.close")}
        data-testid="record-view-share-backdrop"
        onClick={onClose}
        className="absolute inset-0 bg-slate-900/30"
      />
      <div className="relative max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-lg border border-slate-200 bg-white p-5 shadow-xl">
        <div className="flex items-center gap-2">
          <h2
            data-testid="record-view-share-title"
            className="text-sm font-semibold"
          >
            {t("console.recordview.share.dialogTitle")}
          </h2>
          <button
            type="button"
            data-testid="record-view-share-close"
            onClick={onClose}
            className="ml-auto rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50"
          >
            {t("console.dashboard.share.close")}
          </button>
        </div>

        {specError && (
          <p data-testid="record-view-share-error" className="mt-4 text-xs text-red-600">
            {specError}
          </p>
        )}

        {!specError && (
          <>
            {/* 键值选择器：keyColumn 去重下拉（≤500）+ 手输兜底。 */}
            <div data-testid="record-view-share-key" className="mt-4">
              <p className="text-xs font-medium text-slate-600">
                {t("console.recordview.share.keyLabel")}
              </p>
              <div className="mt-1.5 flex flex-wrap items-center gap-2">
                <select
                  data-testid="record-view-share-key-select"
                  value={selectedKey}
                  onChange={(e) => {
                    setSelectedKey(e.target.value);
                    setCopied(false);
                  }}
                  className="rounded border border-slate-300 px-2 py-1 text-sm"
                >
                  <option value="">
                    {t("console.recordview.preview.keyPlaceholder")}
                  </option>
                  {(keyOptions ?? []).map((key) => (
                    <option key={key} value={key}>
                      {key}
                    </option>
                  ))}
                </select>
                <span className="text-[11px] text-slate-400">/</span>
                <input
                  data-testid="record-view-share-key-manual"
                  value={manualKey}
                  onChange={(e) => {
                    setManualKey(e.target.value);
                    setCopied(false);
                  }}
                  placeholder={t("console.recordview.share.manualKeyPlaceholder")}
                  className="w-40 rounded border border-slate-300 px-2 py-1 text-sm"
                />
              </div>
              {keyTruncated && (
                <p className="mt-1 text-[11px] text-amber-600">
                  {t("console.recordview.preview.keysTruncated", {
                    count: RECORD_VIEW_LIMITS.distinctKeys,
                  })}
                </p>
              )}
              {keyLoadError && (
                <p className="mt-1 text-[11px] text-red-600">{keyLoadError}</p>
              )}
            </div>

            {/* 有效期四档（0=不设期·印刷物）——改选即重新生成。 */}
            <div data-testid="record-view-share-ttl" className="mt-4">
              <p className="text-xs font-medium text-slate-600">
                {t("console.dashboard.share.ttlLabel")}
              </p>
              <div
                role="radiogroup"
                aria-label={t("console.dashboard.share.ttlLabel")}
                className="mt-1.5 flex flex-wrap gap-4"
              >
                {RECORD_VIEW_TTL_DAYS.map((days) => (
                  <label
                    key={days}
                    className="flex items-center gap-1 text-xs text-slate-700"
                  >
                    <input
                      type="radio"
                      data-testid="record-view-share-ttl-option"
                      data-days={days}
                      name={`record-view-share-ttl-${artifactId}`}
                      checked={ttlDays === days}
                      onChange={() => setTtlDays(days)}
                    />
                    {t(`console.recordview.share.ttlOptions.${days}`)}
                  </label>
                ))}
              </div>
              <p className="mt-1 text-[11px] text-slate-400">
                {t("console.recordview.share.ttlNote")}
              </p>
              {/* 0 档印刷物提示：不设期 + 撤销语义（归档即失效）。 */}
              {ttlDays === 0 && (
                <p
                  data-testid="record-view-share-perpetual-note"
                  className="mt-1 text-[11px] text-amber-600"
                >
                  {t("console.recordview.share.perpetualNote")}
                </p>
              )}
            </div>

            {shareError && (
              <p data-testid="record-view-share-generate-error" className="mt-4 text-xs text-red-600">
                {shareError}
              </p>
            )}

            {!shareError && result == null && (
              <p data-testid="record-view-share-generating" className="mt-4 text-xs text-slate-400">
                {t("console.dashboard.share.generating")}
              </p>
            )}

            {result != null && (
              <div className="mt-4 space-y-3">
                <div>
                  <p className="text-xs font-medium text-slate-600">
                    {t("console.dashboard.share.urlLabel")}
                  </p>
                  {/* readonly 输入框承载完整链接：可选中复制、长链接不撑破布局。 */}
                  <input
                    data-testid="record-view-share-url"
                    readOnly
                    value={shareUrl}
                    placeholder={t("console.recordview.share.urlKeyHint")}
                    onFocus={(e) => e.currentTarget.select()}
                    className="mt-1 w-full rounded border border-slate-300 bg-slate-50 px-2 py-1.5 font-mono text-[11px] text-slate-700"
                  />
                  <div className="mt-2 flex items-center gap-2">
                    <button
                      type="button"
                      data-testid="record-view-share-copy"
                      disabled={!shareUrl}
                      onClick={() => void copyLink()}
                      className="rounded bg-slate-900 px-3 py-1 text-xs text-white hover:bg-slate-700 disabled:opacity-50"
                    >
                      {t("console.dashboard.share.copy")}
                    </button>
                    {copied && (
                      <span data-testid="record-view-share-copied" className="text-[11px] text-emerald-600">
                        {t("console.dashboard.share.copied")}
                      </span>
                    )}
                  </div>
                </div>
                <p data-testid="record-view-share-expires" className="text-xs text-slate-500">
                  {t("console.dashboard.share.expiresLabel")}：{expiresText}
                </p>
                <p className="text-[11px] text-slate-400">
                  {t("console.dashboard.share.versionNote", { version: result.version })}
                </p>

                {/* 二维码（D5 纯前端）：懒加载库 + PNG 高清/SVG 下载（逐键）。 */}
                <div
                  data-testid="record-view-share-qr"
                  className="rounded-md border border-slate-200 bg-slate-50/60 p-3"
                >
                  <p className="text-xs font-medium text-slate-600">
                    {t("console.recordview.share.qrTitle")}
                  </p>
                  {!shareUrl && (
                    <p className="mt-1.5 text-[11px] text-slate-400">
                      {t("console.recordview.share.qrKeyHint")}
                    </p>
                  )}
                  {shareUrl && qrPng == null && !qrError && (
                    <p
                      data-testid="record-view-share-qr-loading"
                      className="mt-1.5 text-[11px] text-slate-400"
                    >
                      {t("console.recordview.share.qrLoading")}
                    </p>
                  )}
                  {qrError && (
                    <p className="mt-1.5 text-[11px] text-red-600">{qrError}</p>
                  )}
                  {qrPng != null && (
                    <div className="mt-2 flex flex-wrap items-start gap-3">
                      {/* 生成预览（PNG 数据 URL 直呈）。 */}
                      <img
                        data-testid="record-view-share-qr-image"
                        src={qrPng}
                        alt={t("console.recordview.share.qrTitle")}
                        className="h-36 w-36 rounded border border-slate-200 bg-white"
                      />
                      <div className="flex flex-col gap-2">
                        {/* PNG 高清下载（width 1024，印刷足够）。 */}
                        <a
                          data-testid="record-view-share-qr-png"
                          href={qrPng}
                          download={`record-view-${artifactId}-${effectiveKey}.png`}
                          className="rounded bg-slate-900 px-3 py-1 text-center text-xs text-white hover:bg-slate-700"
                        >
                          {t("console.recordview.share.qrDownloadPng")}
                        </a>
                        {/* SVG 矢量下载（Blob URL 程序化触发）。 */}
                        <button
                          type="button"
                          data-testid="record-view-share-qr-svg"
                          disabled={qrSvg == null}
                          onClick={downloadSvg}
                          className="rounded border border-slate-300 bg-white px-3 py-1 text-xs text-slate-600 hover:bg-slate-100 disabled:opacity-50"
                        >
                          {t("console.recordview.share.qrDownloadSvg")}
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
