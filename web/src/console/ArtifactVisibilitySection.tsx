// 产物公开分享选择器（wire-artifact-marketplace 任务 5.1）：产物列表页内每个
// published 产物的「公开分享」区块——四档可见性（private 默认 / link /
// directed / market）segmented radio + 开放模式（open 订阅即看 / approval
// 需发布方批准）+ 「允许复制」开关（allow_copy，默认关）。
//
// 与数据集侧 DatasetVisibilitySection 同构（交互范式逐条对齐）；本批差异：
// - 无敏感度 prop：绑定数据集敏感档闸在服务端（设 market 时 422、渲染时复检
//   ——Q5 双点闸），前端在选到 market 档时以 sensitiveNote 预告该纪律；
// - 非 published 产物不可设非 private 档（前端置灰 + publishHint；服务端
//   409 detail 兜底——双保险）；
// - Q1 硬要求：公开看板/报告包含发布时刻的数据快照——visibilityPublicNote
//   常驻披露（禁止「仅结构不含数据」措辞）。
// i18n：console.artifact.visibility.*（zh/en 键位对仗，i18nKeys.test 把关）。
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { API_BASE } from "../catalog/api";
import { apiErrorText, ConsoleApiError } from "./api";
import { loadSession } from "./auth";
import { formatServerDate } from "./util";

/** 可见性四档阶梯（private 默认 → link → directed → market）。 */
export const ARTIFACT_VISIBILITIES = [
  "private",
  "link",
  "directed",
  "market",
] as const;

export type ArtifactVisibility = (typeof ARTIFACT_VISIBILITIES)[number];

/** 开放模式（仅 directed/market 档有意义）：订阅即看 / 需发布方批准。 */
export const ARTIFACT_ACCESS_MODES = ["open", "approval"] as const;

export type ArtifactAccessMode = (typeof ARTIFACT_ACCESS_MODES)[number];

/** 可见性设置响应（GET/PUT 同形；published_at/version 仅开放后非 null）。 */
export interface ArtifactVisibilityState {
  visibility: ArtifactVisibility;
  access_mode: ArtifactAccessMode;
  allow_copy: boolean;
  published_at: string | null;
  /** 开放时刻的版本锚点（服务端数字版本号；未开放为 null）。 */
  published_version: string | number | null;
}

/** 可见性设置入参（PUT 全量三字段）。 */
export interface ArtifactVisibilityInput {
  visibility: ArtifactVisibility;
  access_mode: ArtifactAccessMode;
  allow_copy: boolean;
}

/** 本区块专属端点的轻量请求器（api.ts requestJson 同语义：Bearer 会话头、
 *  非 2xx 抛 ConsoleApiError（带后端 detail 文案，如 422 敏感档闸 / 409
 *  未发布）、204 → null）。 */
async function artifactVisibilityRequest(
  path: string,
  method: "GET" | "PUT",
  body?: unknown,
): Promise<unknown> {
  const session = loadSession();
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      headers: {
        Accept: "application/json",
        ...(session?.idToken
          ? { Authorization: `Bearer ${session.idToken}` }
          : {}),
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    });
  } catch (err) {
    throw new ConsoleApiError(
      `network error fetching ${path}: ${(err as Error).message}`,
    );
  }
  if (!response.ok) {
    let detail: string | undefined;
    try {
      const data: unknown = await response.json();
      if (
        data &&
        typeof data === "object" &&
        typeof (data as { detail?: unknown }).detail === "string"
      ) {
        detail = (data as { detail: string }).detail;
      }
    } catch {
      /* 无 JSON body：保留 undefined（走兜底文案） */
    }
    throw new ConsoleApiError(
      `artifact visibility api ${response.status} for ${path}`,
      response.status,
      detail,
    );
  }
  if (response.status === 204) return null;
  return response.json();
}

/** 响应收窄（缺省/非法值保守回落 private + open——默认私有铁律镜像）。 */
function normalizeVisibilityState(data: unknown): ArtifactVisibilityState {
  const record = (data ?? {}) as Record<string, unknown>;
  const visibility = ARTIFACT_VISIBILITIES.includes(
    record.visibility as ArtifactVisibility,
  )
    ? (record.visibility as ArtifactVisibility)
    : "private";
  const accessMode = ARTIFACT_ACCESS_MODES.includes(
    record.access_mode as ArtifactAccessMode,
  )
    ? (record.access_mode as ArtifactAccessMode)
    : "open";
  return {
    visibility,
    access_mode: accessMode,
    allow_copy: record.allow_copy === true,
    published_at:
      typeof record.published_at === "string" ? record.published_at : null,
    published_version:
      (record.published_version as string | number | null) ?? null,
  };
}

/** 当前可见性态（GET /artifacts/{id}/visibility）。 */
async function fetchArtifactVisibility(
  artifactId: number,
): Promise<ArtifactVisibilityState> {
  return normalizeVisibilityState(
    await artifactVisibilityRequest(
      `/v1/artifacts/${artifactId}/visibility`,
      "GET",
    ),
  );
}

/** 设置可见性（PUT 全量三字段；422/409 detail 由调用方如实呈现）。 */
async function updateArtifactVisibility(
  artifactId: number,
  payload: ArtifactVisibilityInput,
): Promise<ArtifactVisibilityState> {
  return normalizeVisibilityState(
    await artifactVisibilityRequest(
      `/v1/artifacts/${artifactId}/visibility`,
      "PUT",
      payload,
    ),
  );
}

export function ArtifactVisibilitySection({
  artifactId,
  artifactStatus,
  canEdit,
}: {
  artifactId: number;
  /** 产物状态（draft/published/archived）：非 published 时非 private 档置灰。 */
  artifactStatus: string;
  /** false = 只读档：隐藏保存入口、禁用全部控件（服务端 403 双保险）。 */
  canEdit: boolean;
}) {
  const { t } = useTranslation();
  const [state, setState] = useState<ArtifactVisibilityState | null>(null);
  const [draft, setDraft] = useState<ArtifactVisibility>("private");
  const [draftMode, setDraftMode] = useState<ArtifactAccessMode>("open");
  const [draftCopy, setDraftCopy] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [opError, setOpError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState(false);

  const load = useCallback(async () => {
    setLoadError(false);
    setOpError(null);
    try {
      const next = await fetchArtifactVisibility(artifactId);
      setState(next);
      setDraft(next.visibility);
      setDraftMode(next.access_mode);
      setDraftCopy(next.allow_copy);
    } catch {
      setLoadError(true);
    }
  }, [artifactId]);

  useEffect(() => {
    setState(null);
    setSaved(false);
    void load();
  }, [load]);

  /** 保存（PUT /visibility 主端点；四档 + 开放模式 + 允许复制全量提交）。 */
  const save = async () => {
    if (saving || !canEdit) return;
    setSaving(true);
    setSaved(false);
    setOpError(null);
    try {
      const next = await updateArtifactVisibility(artifactId, {
        visibility: draft,
        access_mode: draftMode,
        allow_copy: draftCopy,
      });
      setState(next);
      setDraft(next.visibility);
      setDraftMode(next.access_mode);
      setDraftCopy(next.allow_copy);
      setSaved(true);
    } catch (err) {
      // 422（敏感档闸等）/409（未发布等）后端 detail 如实回显。
      setOpError(
        apiErrorText(err, t("console.artifact.visibility.actionError")),
      );
    } finally {
      setSaving(false);
    }
  };

  const modeActive = draft === "market" || draft === "directed";
  const dirty =
    state != null &&
    (state.visibility !== draft ||
      state.allow_copy !== draftCopy ||
      (modeActive && state.access_mode !== draftMode));
  const notPublished = artifactStatus !== "published";

  return (
    <section data-testid="artifact-visibility-section" className="mt-6">
      <h2 className="text-base font-semibold">
        {t("console.artifact.visibility.title")}
      </h2>
      <p className="mt-1 max-w-2xl text-xs text-slate-500">
        {t("console.artifact.visibility.hint")}
      </p>

      {loadError && (
        <div
          data-testid="artifact-visibility-error"
          className="mt-2 rounded-md border border-red-200 bg-red-50 p-3"
        >
          <p className="text-xs text-red-700">
            {t("console.artifact.visibility.loadError")}
          </p>
          <button
            type="button"
            data-testid="artifact-visibility-retry"
            onClick={() => void load()}
            className="mt-2 rounded border border-red-300 px-2 py-0.5 text-xs text-red-700 hover:bg-red-100"
          >
            {/* 重试按钮复用产物页既有键（本批不为重试单开键）。 */}
            {t("console.artifacts.retry")}
          </button>
        </div>
      )}

      {!loadError && state == null && (
        <p
          data-testid="artifact-visibility-loading"
          className="mt-2 text-xs text-slate-400"
        >
          {t("console.artifact.visibility.loading")}
        </p>
      )}

      {!loadError && state != null && (
        <>
          {/* 当前态摘要：四档徽标 + 开放模式 + 开放时间（非 private 后）。 */}
          <div
            data-testid="artifact-visibility-current"
            data-visibility={state.visibility}
            className="mt-2 flex flex-wrap items-center gap-2 text-xs"
          >
            <span
              data-testid="artifact-visibility-current-badge"
              className={`rounded-full px-2 py-0.5 ring-1 ${
                state.visibility === "private"
                  ? "bg-slate-100 text-slate-600 ring-slate-200"
                  : state.visibility === "market"
                    ? "bg-emerald-50 text-emerald-700 ring-emerald-200"
                    : "bg-indigo-50 text-indigo-700 ring-indigo-200"
              }`}
            >
              {t(`console.artifact.visibility.levels.${state.visibility}`)}
            </span>
            {(state.visibility === "market" ||
              state.visibility === "directed") && (
              <span className="text-slate-500">
                {t("console.artifact.visibility.accessModeLabel")}：
                {t(`console.artifact.visibility.accessMode.${state.access_mode}`)}
              </span>
            )}
            {state.published_at != null && (
              <span
                data-testid="artifact-published-at"
                className="text-slate-400"
              >
                {t("console.artifact.visibility.publishedAtLabel")}
                {formatServerDate(state.published_at)}
              </span>
            )}
          </div>

          {/* 四档 segmented radio：private 默认；非 published 产物其余档置灰。 */}
          <div
            data-testid="artifact-visibility-select"
            role="radiogroup"
            className="mt-2 inline-flex flex-wrap items-center gap-1 rounded-lg border border-slate-200 bg-slate-50/60 p-1"
          >
            {ARTIFACT_VISIBILITIES.map((level) => {
              const gated = notPublished && level !== "private";
              const disabled = gated || saving || !canEdit;
              return (
                <button
                  key={level}
                  type="button"
                  role="radio"
                  aria-checked={draft === level}
                  data-testid={`artifact-visibility-option-${level}`}
                  data-level={level}
                  data-disabled={gated ? "true" : "false"}
                  disabled={disabled}
                  onClick={() => {
                    setDraft(level);
                    setSaved(false);
                  }}
                  className={`rounded-md px-3 py-1 text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${
                    draft === level
                      ? "bg-slate-900 text-white"
                      : "text-slate-600 hover:bg-slate-100"
                  }`}
                >
                  {t(`console.artifact.visibility.levels.${level}`)}
                </button>
              );
            })}
          </div>

          {/* 非 published：非 private 档不可选（前端置灰 + 说明）。 */}
          {notPublished && (
            <p
              data-testid="artifact-visibility-publish-hint"
              className="mt-1.5 max-w-2xl rounded border border-amber-200 bg-amber-50/60 px-2.5 py-1.5 text-[11px] text-amber-700"
            >
              {t("console.artifact.visibility.publishHint")}
            </p>
          )}

          {/* market 档纪律预告：绑定数据集含敏感档时服务端 422（Q5 硬闸）。 */}
          {draft === "market" && (
            <p
              data-testid="artifact-visibility-sensitive-note"
              className="mt-1.5 max-w-2xl rounded border border-amber-200 bg-amber-50/60 px-2.5 py-1.5 text-[11px] text-amber-700"
            >
              {t("console.artifact.visibility.sensitiveNote")}
            </p>
          )}

          {/* 开放模式（directed/market 档显示）：open 订阅即看 / approval 需批准。 */}
          {modeActive && (
            <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
              <span className="text-slate-500">
                {t("console.artifact.visibility.accessModeLabel")}：
              </span>
              <div
                data-testid="artifact-access-mode"
                role="radiogroup"
                className="inline-flex items-center gap-1 rounded-lg border border-slate-200 bg-slate-50/60 p-1"
              >
                {ARTIFACT_ACCESS_MODES.map((mode) => (
                  <button
                    key={mode}
                    type="button"
                    role="radio"
                    aria-checked={draftMode === mode}
                    data-testid={`artifact-access-mode-option-${mode}`}
                    disabled={saving || !canEdit}
                    onClick={() => {
                      setDraftMode(mode);
                      setSaved(false);
                    }}
                    className={`rounded-md px-2.5 py-1 transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${
                      draftMode === mode
                        ? "bg-slate-900 text-white"
                        : "text-slate-600 hover:bg-slate-100"
                    }`}
                  >
                    {t(`console.artifact.visibility.accessMode.${mode}`)}
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* 允许复制开关（发布方可选：订阅者能否把该产物复制到自己账号）。 */}
          <label className="mt-2 flex items-center gap-2 text-xs text-slate-600">
            <input
              type="checkbox"
              data-testid="artifact-allow-copy"
              checked={draftCopy}
              disabled={saving || !canEdit}
              onChange={(event) => {
                setDraftCopy(event.target.checked);
                setSaved(false);
              }}
              className="h-3.5 w-3.5 rounded border-slate-300"
            />
            {t("console.artifact.visibility.allowCopyLabel")}
          </label>
          <p className="mt-1 max-w-2xl text-[11px] text-slate-400">
            {t("console.artifact.visibility.allowCopyHint")}
          </p>

          {canEdit && (
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <button
                type="button"
                data-testid="artifact-visibility-save"
                disabled={saving || !dirty}
                onClick={() => void save()}
                className="rounded bg-slate-900 px-3 py-1 text-xs text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-300"
              >
                {saving
                  ? t("console.artifact.visibility.saving")
                  : t("console.artifact.visibility.save")}
              </button>
              {saved && !opError && (
                <span
                  data-testid="artifact-visibility-saved"
                  className="text-xs text-emerald-600"
                >
                  {t("console.artifact.visibility.saved")}
                </span>
              )}
            </div>
          )}

          {opError && (
            <p
              data-testid="artifact-visibility-error"
              className="mt-2 text-xs text-red-600"
            >
              {opError}
            </p>
          )}
        </>
      )}

      {/* Q1 如实披露（常驻——加载中/成功后/失败态都在场，硬要求）。 */}
      <p
        data-testid="artifact-visibility-note"
        className="mt-3 max-w-2xl rounded border border-sky-200 bg-sky-50/60 px-2.5 py-1.5 text-[11px] text-sky-700"
      >
        {t("console.artifact.visibility.visibilityPublicNote")}
      </p>
    </section>
  );
}
