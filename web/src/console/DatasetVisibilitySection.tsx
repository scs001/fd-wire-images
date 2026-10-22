// 数据开放选择器（wire-dataset-marketplace 任务 6.1）：数据集详情页内的
// 「数据开放」区块——四档可见性（private 默认 / link / directed / market）
// segmented radio + 开放模式（open 订阅即得 / approval 需批准）+ directed 档
// 客户授权名单编辑（增删 customer_id）。
// 敏感档约束（硬闸镜像）：数据集敏感度=「敏感」时 market/directed 两档禁用，
// 说明「敏感数据只支持私有或链接分享」——服务端 422 兜底（双保险）。
// 一键快捷：market 档「发布到市场」按钮（=PUT visibility market）、
// private 档「撤回开放」（=unpublish→private）。开放可撤回、留痕在服务端
// asset_meta_events（本组件不重复呈现留痕——资产信息区已有留痕面）。
// i18n：console.dataset.visibility.*（zh/en 键位对仗，i18nKeys.test 把关）。
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  DATASET_ACCESS_MODES,
  DATASET_VISIBILITIES,
  addDatasetGrant,
  apiErrorText,
  fetchDatasetGrants,
  fetchDatasetVisibility,
  publishDatasetToMarket,
  removeDatasetGrant,
  unpublishDatasetFromMarket,
  updateDatasetVisibility,
  type DatasetAccessMode,
  type DatasetGrant,
  type DatasetVisibility,
  type DatasetVisibilityState,
} from "./api";

/** 从资产元数据敏感度判断是否禁开市场/定向（敏感档只允许 private/link）。 */
export function sensitivityBlocksMarket(
  sensitivity: string | null | undefined,
): boolean {
  return sensitivity === "敏感";
}

export function DatasetVisibilitySection({
  datasetId,
  sensitivity,
}: {
  datasetId: string;
  /** 数据集敏感度（资产元数据段；未拉到时按常规处理——禁用闸服务端兜底）。 */
  sensitivity: string | null | undefined;
}) {
  const { t } = useTranslation();
  const [state, setState] = useState<DatasetVisibilityState | null>(null);
  const [grants, setGrants] = useState<DatasetGrant[] | null>(null);
  const [draft, setDraft] = useState<DatasetVisibility>("private");
  const [draftMode, setDraftMode] = useState<DatasetAccessMode>("open");
  const [newGrant, setNewGrant] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [opError, setOpError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [grantsExpanded, setGrantsExpanded] = useState(false);
  void grantsExpanded; // 展开态暂仅驱动视觉，后续 grant 编辑器折叠时消费

  const load = useCallback(async () => {
    setLoadError(false);
    try {
      const [visibility, grantList] = await Promise.all([
        fetchDatasetVisibility(datasetId),
        fetchDatasetGrants(datasetId).catch(() => [] as DatasetGrant[]),
      ]);
      setState(visibility);
      setGrants(grantList);
      setDraft(visibility.visibility);
      setDraftMode(visibility.access_mode);
      setGrantsExpanded(visibility.visibility === "directed");
    } catch {
      setLoadError(true);
    }
  }, [datasetId]);

  useEffect(() => {
    setState(null);
    setGrants(null);
    void load();
  }, [load]);

  /** 保存（PUT visibility 主端点；directed 档随体带 grants 全量名单）。 */
  const save = async () => {
    if (saving) return;
    setSaving(true);
    setSaved(false);
    setOpError(null);
    try {
      const next = await updateDatasetVisibility(datasetId, {
        visibility: draft,
        access_mode: draftMode,
        ...(draft === "directed"
          ? { grants: (grants ?? []).map((g) => g.customer_id) }
          : {}),
      });
      setState(next);
      setSaved(true);
    } catch (err) {
      setOpError(
        apiErrorText(err, t("console.dataset.visibility.saveError")),
      );
    } finally {
      setSaving(false);
    }
  };

  /** 一键发布到市场（薄封装 = market 档；敏感档由服务端 422 拒收）。 */
  const publish = async () => {
    if (saving) return;
    setSaving(true);
    setSaved(false);
    setOpError(null);
    try {
      const next = await publishDatasetToMarket(datasetId);
      setState(next);
      setDraft(next.visibility);
      setDraftMode(next.access_mode);
      setSaved(true);
    } catch (err) {
      setOpError(
        apiErrorText(err, t("console.dataset.visibility.saveError")),
      );
    } finally {
      setSaving(false);
    }
  };

  /** 撤回开放（回私有；任何档位随时可做——可撤回纪律）。 */
  const unpublish = async () => {
    if (saving) return;
    setSaving(true);
    setSaved(false);
    setOpError(null);
    try {
      const next = await unpublishDatasetFromMarket(datasetId);
      setState(next);
      setDraft(next.visibility);
      setSaved(true);
    } catch (err) {
      setOpError(
        apiErrorText(err, t("console.dataset.visibility.saveError")),
      );
    } finally {
      setSaving(false);
    }
  };

  const addGrant = async () => {
    const customerId = newGrant.trim();
    if (!customerId || saving) return;
    setSaving(true);
    setOpError(null);
    try {
      await addDatasetGrant(datasetId, customerId);
      setGrants(await fetchDatasetGrants(datasetId));
      setNewGrant("");
    } catch (err) {
      setOpError(
        apiErrorText(err, t("console.dataset.visibility.saveError")),
      );
    } finally {
      setSaving(false);
    }
  };

  const removeGrant = async (customerId: string) => {
    if (saving) return;
    setSaving(true);
    setOpError(null);
    try {
      await removeDatasetGrant(datasetId, customerId);
      setGrants(await fetchDatasetGrants(datasetId));
    } catch (err) {
      setOpError(
        apiErrorText(err, t("console.dataset.visibility.saveError")),
      );
    } finally {
      setSaving(false);
    }
  };

  const blocked = sensitivityBlocksMarket(sensitivity);
  const dirty =
    state != null &&
    (state.visibility !== draft ||
      (draft === "market" || draft === "directed"
        ? state.access_mode !== draftMode
        : false));

  if (loadError) {
    return (
      <section data-testid="visibility-section" className="mt-6">
        <h2 className="text-base font-semibold">
          {t("console.dataset.visibility.title")}
        </h2>
        <div
          data-testid="visibility-error"
          className="mt-2 rounded-md border border-red-200 bg-red-50 p-3"
        >
          <p className="text-xs text-red-700">
            {t("console.dataset.visibility.loadError")}
          </p>
          <button
            type="button"
            data-testid="visibility-retry"
            onClick={() => void load()}
            className="mt-2 rounded border border-red-300 px-2 py-0.5 text-xs text-red-700 hover:bg-red-100"
          >
            {t("console.dataset.visibility.retry")}
          </button>
        </div>
      </section>
    );
  }

  return (
    <section data-testid="visibility-section" className="mt-6">
      <h2 className="text-base font-semibold">
        {t("console.dataset.visibility.title")}
      </h2>
      <p className="mt-1 max-w-2xl text-xs text-slate-500">
        {t("console.dataset.visibility.hint")}
      </p>

      {state == null && (
        <p data-testid="visibility-loading" className="mt-2 text-xs text-slate-400">
          {t("console.dataset.visibility.loading")}
        </p>
      )}

      {state != null && (
        <>
          {/* 当前态摘要：四档徽标 + 开放模式 + 发布时间（published 后）。 */}
          <div
            data-testid="visibility-current"
            data-visibility={state.visibility}
            className="mt-2 flex flex-wrap items-center gap-2 text-xs"
          >
            <span
              data-testid="visibility-current-badge"
              className={`rounded-full px-2 py-0.5 ring-1 ${
                state.visibility === "private"
                  ? "bg-slate-100 text-slate-600 ring-slate-200"
                  : state.visibility === "market"
                    ? "bg-emerald-50 text-emerald-700 ring-emerald-200"
                    : "bg-indigo-50 text-indigo-700 ring-indigo-200"
              }`}
            >
              {t(
                `console.dataset.visibility.levels.${state.visibility}`,
              )}
            </span>
            {(state.visibility === "market" ||
              state.visibility === "directed") && (
              <span className="text-slate-500">
                {t("console.dataset.visibility.accessModeLabel")}：
                {t(`console.dataset.accessMode.${state.access_mode}`)}
              </span>
            )}
            {state.published_at != null && (
              <span data-testid="visibility-published-at" className="text-slate-400">
                {t("console.dataset.visibility.publishedAt", {
                  at: state.published_at,
                })}
              </span>
            )}
          </div>

          {/* 四档 segmented radio：private 默认；敏感档 market/directed 禁用。 */}
          <div
            data-testid="dataset-visibility-select"
            role="radiogroup"
            className="mt-2 inline-flex flex-wrap items-center gap-1 rounded-lg border border-slate-200 bg-slate-50/60 p-1"
          >
            {DATASET_VISIBILITIES.map((level) => {
              const disabled = blocked && (level === "market" || level === "directed");
              return (
                <button
                  key={level}
                  type="button"
                  role="radio"
                  aria-checked={draft === level}
                  data-testid={`visibility-option-${level}`}
                  data-level={level}
                  data-disabled={disabled ? "true" : "false"}
                  disabled={disabled || saving}
                  title={
                    disabled
                      ? t("console.dataset.visibility.sensitiveBlockedTitle")
                      : undefined
                  }
                  onClick={() => {
                    setDraft(level);
                    setSaved(false);
                    if (level === "directed") setGrantsExpanded(true);
                  }}
                  className={`rounded-md px-3 py-1 text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${
                    draft === level
                      ? "bg-slate-900 text-white"
                      : "text-slate-600 hover:bg-slate-100"
                  }`}
                >
                  {t(`console.dataset.visibility.levels.${level}`)}
                </button>
              );
            })}
          </div>
          <p className="mt-1 max-w-2xl text-[11px] text-slate-400">
            {t(`console.dataset.visibility.levelHints.${draft}`)}
          </p>

          {/* 敏感档约束说明：market/directed 被禁用时呈现（服务端 422 双保险）。 */}
          {blocked && (
            <p
              data-testid="visibility-sensitive-note"
              className="mt-1.5 max-w-2xl rounded border border-amber-200 bg-amber-50/60 px-2.5 py-1.5 text-[11px] text-amber-700"
            >
              {t("console.dataset.visibility.sensitiveNote")}
            </p>
          )}

          {/* 开放模式（market/directed 档展开）：open 订阅即得 / approval 需批准。 */}
          {(draft === "market" || draft === "directed") && (
            <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
              <span className="text-slate-500">
                {t("console.dataset.visibility.accessModeLabel")}：
              </span>
              <div
                data-testid="visibility-access-mode"
                role="radiogroup"
                className="inline-flex items-center gap-1 rounded-lg border border-slate-200 bg-slate-50/60 p-1"
              >
                {DATASET_ACCESS_MODES.map((mode) => (
                  <button
                    key={mode}
                    type="button"
                    role="radio"
                    aria-checked={draftMode === mode}
                    data-testid={`visibility-mode-${mode}`}
                    disabled={saving}
                    onClick={() => {
                      setDraftMode(mode);
                      setSaved(false);
                    }}
                    className={`rounded-md px-2.5 py-1 transition-colors ${
                      draftMode === mode
                        ? "bg-slate-900 text-white"
                        : "text-slate-600 hover:bg-slate-100"
                    }`}
                  >
                    {t(`console.dataset.accessMode.${mode}`)}
                  </button>
                ))}
              </div>
              <span className="text-[11px] text-slate-400">
                {t(`console.dataset.visibility.modeHints.${draftMode}`)}
              </span>
            </div>
          )}

          {/* directed 档：客户授权名单编辑（增删 customer_id）。 */}
          {draft === "directed" && (
            <div
              data-testid="dataset-grant-editor"
              className="mt-2 rounded-lg border border-slate-200 bg-white p-3 text-xs"
            >
              <p className="text-slate-500">
                {t("console.dataset.visibility.grantsTitle")}
              </p>
              {(grants ?? []).length === 0 ? (
                <p
                  data-testid="visibility-grants-empty"
                  className="mt-1.5 text-[11px] text-slate-400"
                >
                  {t("console.dataset.visibility.grantsEmpty")}
                </p>
              ) : (
                <ul data-testid="visibility-grants-list" className="mt-1.5 space-y-1">
                  {(grants ?? []).map((grant) => (
                    <li
                      key={grant.customer_id}
                      data-testid="visibility-grant-row"
                      data-customer={grant.customer_id}
                      className="flex items-center gap-2"
                    >
                      <span className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-700">
                        {grant.customer_id}
                      </span>
                      <button
                        type="button"
                        data-testid="visibility-grant-remove"
                        disabled={saving}
                        onClick={() => void removeGrant(grant.customer_id)}
                        className="rounded border border-slate-300 px-1.5 py-0.5 text-[10px] text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
                      >
                        {t("console.dataset.visibility.grantRemove")}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              <div className="mt-2 flex items-center gap-2">
                <input
                  data-testid="visibility-grant-input"
                  value={newGrant}
                  onChange={(e) => setNewGrant(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") void addGrant();
                  }}
                  placeholder={t("console.dataset.visibility.grantPlaceholder")}
                  className="w-48 rounded border border-slate-300 px-2 py-1 font-mono text-[11px]"
                />
                <button
                  type="button"
                  data-testid="visibility-grant-add"
                  disabled={saving || !newGrant.trim()}
                  onClick={() => void addGrant()}
                  className="rounded border border-slate-300 px-2 py-1 text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {t("console.dataset.visibility.grantAdd")}
                </button>
              </div>
            </div>
          )}

          {/* 操作条：保存 / market 快捷 / private 撤回快捷。 */}
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <button
              type="button"
              data-testid="dataset-visibility-save"
              disabled={saving || !dirty}
              onClick={() => void save()}
              className="rounded bg-slate-900 px-3 py-1 text-xs text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-300"
            >
              {saving
                ? t("console.dataset.visibility.saving")
                : t("console.dataset.visibility.save")}
            </button>
            {/* market 快捷：非 market 档且未禁用 → 发布到市场。 */}
            {draft === "market" && state.visibility !== "market" && (
              <button
                type="button"
                data-testid="dataset-market-publish"
                disabled={saving}
                onClick={() => void publish()}
                className="rounded bg-emerald-600 px-3 py-1 text-xs text-white hover:bg-emerald-700 disabled:cursor-not-allowed disabled:bg-emerald-300"
              >
                {t("console.dataset.visibility.publish")}
              </button>
            )}
            {/* 撤回快捷：已开放（非 private 草稿或服务端已是开放态）→ 回私有。 */}
            {(state.visibility !== "private" || draft !== "private") && (
              <button
                type="button"
                data-testid="dataset-market-unpublish"
                disabled={saving}
                onClick={() => {
                  setDraft("private");
                  if (state.visibility !== "private") void unpublish();
                }}
                className="rounded border border-slate-300 px-3 py-1 text-xs text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {t("console.dataset.visibility.unpublish")}
              </button>
            )}
            {saved && !opError && (
              <span data-testid="visibility-saved" className="text-xs text-emerald-600">
                {t("console.dataset.visibility.saved")}
              </span>
            )}
          </div>

          {opError && (
            <p data-testid="visibility-op-error" className="mt-2 text-xs text-red-600">
              {opError}
            </p>
          )}
        </>
      )}
    </section>
  );
}
