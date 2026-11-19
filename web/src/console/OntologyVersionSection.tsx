// 本体版本区（add-ontology-versioning 任务 5.2 web 面）：实体页顶部独立区块。
// 三块能力 + 一个自检入口：
// - 版本时间线（倒序）：版本号 / digest 前 8 / trigger 徽标（日版/发布版）/
//   label / 实体·关系规模计数 / 本地化时间；行内「对比当前」「导出」与
//   owner-only「回滚」；
// - 未发布变更计数（=最近快照之后的事件数；无快照时计全部事件）+ 「打发布
//   版」（label 可选 ≤80 字；同 digest 时后端返回 unchanged，UI 如实说明
//   未新增版本，绝不谎称新版本）；
// - diff 面板：实体/关系各三组（新增/删除/字段级改动 before→after）；
// - 回滚：先 POST rollback/plan 预览（create/update/delete 计数 + update 的
//   字段级差异），owner 再点确认才执行；422 的 detail 是对象
//   {message,executed,note}——取 message 原文如实显示（绝不 [object Object]）；
// - 完整性自检：一致=绿、不一致=红并展开差异条数与 escape_hint。
// 角色源 = 既有团队面 GET /api/console/team 的 me.role（未拉到按不可见处理）；
// 版本区独立拉自己的数据，实体页空态时照常显示。
// i18n：console.entities.version.*（zh/en 键位对仗，i18nKeys.test 把关）。
// polish-wire-ontology-console 打磨（呈现层 only、行为无关）：加载占位
// min-height 不闪跳；空态=虚线卡主/次句引导；错误卡带 HTTP 状态码（mono）；
// diff 语义色沿 brief token（emerald 增 / rose 删 / amber 改·unchanged）、
// 关系标识机器读 font-mono；宽 diff 横向滚动；回滚计划加危险提示。
// 新键 emptyHint / rollbackWarning 已归并进 i18n/zh.ts、en.ts（中央归并波），
// 组件直走 t()。
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  ConsoleApiError,
  apiErrorText,
  downloadOntologyExport,
  executeOntologyRollback,
  fetchOntologyDiff,
  fetchOntologyIntegrity,
  fetchOntologyRollbackPlan,
  fetchOntologyVersions,
  getTeam,
  publishOntologyVersion,
  type OntologyDiff,
  type OntologyDiffBody,
  type OntologyDiffGroup,
  type OntologyEntityRecord,
  type OntologyExportFormat,
  type OntologyFieldChange,
  type OntologyIntegrity,
  type OntologyPublishResult,
  type OntologyRelationshipRecord,
  type OntologyRollbackPlan,
  type OntologyRollbackResult,
  type OntologyUnpublished,
  type OntologyVersionSummary,
  type TeamRole,
} from "./api";

// ---- 本轮新增文案已归并 i18n（polish-wire-ontology-console 中央归并波）----
// console.entities.version.emptyHint / rollbackWarning 现直接读 zh/en 资源。

/** 字段值展示：null/undefined → —；数组/对象 → 紧凑 JSON；其余原样。 */
function fieldText(value: unknown): string {
  if (value == null) return "—";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return JSON.stringify(value);
}

/** 本地化时间（坏值原样回显，不显示 Invalid Date）。 */
function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

/** 实体差异条目可读标签。 */
function entityLabel(item: OntologyEntityRecord): string {
  return `${item.name} (#${item.id})`;
}

/** 关系差异条目可读标签（除四元组外带关系 id）。 */
function relationshipLabel(item: OntologyRelationshipRecord): string {
  return `#${item.id} ${item.from_entity_id}.${item.from_column} → ${item.to_entity_id}.${item.to_column}`;
}

/** 差异总条数（实体三组 + 关系三组）。 */
function diffTotal(diff: OntologyDiffBody): number {
  return diff.counts.entities.total + diff.counts.relationships.total;
}

/** 回滚失败载荷：422 对象 detail 取 message 原文 + executed 步数 + note；
 *  字符串 detail 走既有口径；绝不出现 [object Object]。 */
function parseRollbackFailure(
  err: unknown,
  fallback: string,
): { message: string; executed: number | null; note: string | null } {
  if (
    err instanceof ConsoleApiError &&
    err.detail &&
    typeof err.detail === "object"
  ) {
    const detail = err.detail;
    const message =
      typeof detail.message === "string" && detail.message
        ? detail.message
        : fallback;
    const executed = Array.isArray(detail.executed)
      ? detail.executed.length
      : null;
    const note =
      typeof detail.note === "string" && detail.note ? detail.note : null;
    return { message, executed, note };
  }
  return { message: apiErrorText(err, fallback), executed: null, note: null };
}

/**
 * 差异一组（增/删/字段级改）——diff 面板与完整性差异共用。
 * 语义色沿 console token：emerald=新增 / rose=删除 / amber=字段级改；
 * itemMono=条目标识是机器读（关系四元组/列名）时加 font-mono。
 * min-w-max + 外层 overflow-x-auto：宽 diff 不破版、横向滚动。
 */
function DiffGroupView<
  TItem,
  TChange extends { fields: Record<string, OntologyFieldChange> },
>({
  testId,
  titleKey,
  group,
  itemLabel,
  changeLabel,
  itemMono = false,
}: {
  testId: string;
  titleKey: string;
  group: OntologyDiffGroup<TItem, TChange>;
  itemLabel: (item: TItem) => string;
  changeLabel: (change: TChange) => string;
  itemMono?: boolean;
}) {
  const { t } = useTranslation();
  const total = group.added.length + group.removed.length + group.changed.length;
  return (
    <div
      data-testid={testId}
      className="min-w-max rounded border border-slate-200 bg-slate-50 p-2"
    >
      <p className="text-xs font-medium text-slate-700">{t(titleKey)}</p>
      {total === 0 ? (
        <p
          data-testid={`${testId}-none`}
          className="mt-1 text-[11px] text-slate-400"
        >
          {t("console.entities.version.none")}
        </p>
      ) : (
        <div className="mt-1 space-y-1">
          {group.added.length > 0 && (
            <div data-testid={`${testId}-added`}>
              <p className="text-[11px] text-emerald-700">
                {t("console.entities.version.added", {
                  count: group.added.length,
                })}
              </p>
              <ul className="ml-3 list-disc text-[11px] text-slate-600">
                {group.added.map((item, index) => (
                  <li
                    key={`${testId}-added-${index}`}
                    className={itemMono ? "font-mono" : undefined}
                  >
                    + {itemLabel(item)}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {group.removed.length > 0 && (
            <div data-testid={`${testId}-removed`}>
              <p className="text-[11px] text-rose-700">
                {t("console.entities.version.removed", {
                  count: group.removed.length,
                })}
              </p>
              <ul className="ml-3 list-disc text-[11px] text-slate-600">
                {group.removed.map((item, index) => (
                  <li
                    key={`${testId}-removed-${index}`}
                    className={itemMono ? "font-mono" : undefined}
                  >
                    − {itemLabel(item)}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {group.changed.length > 0 && (
            <div data-testid={`${testId}-changed`}>
              <p className="text-[11px] text-amber-700">
                {t("console.entities.version.changed", {
                  count: group.changed.length,
                })}
              </p>
              <ul className="ml-3 space-y-1 text-[11px] text-slate-600">
                {group.changed.map((change, index) => (
                  <li key={`${testId}-changed-${index}`}>
                    <span className={itemMono ? "font-mono" : undefined}>
                      {changeLabel(change)}
                    </span>
                    <ul className="ml-3 space-y-0.5">
                      {Object.entries(change.fields).map(([field, delta]) => (
                        <li
                          key={field}
                          data-testid={`${testId}-field`}
                          data-field={field}
                          className="font-mono text-[11px] text-slate-500"
                        >
                          {field}: {fieldText(delta.before)} →{" "}
                          {fieldText(delta.after)}
                        </li>
                      ))}
                    </ul>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function OntologyVersionSection() {
  const { t } = useTranslation();
  // 角色源 = 既有团队面；未拉到时按「不可见」处理（回滚入口绝不提前渲染）。
  const [role, setRole] = useState<TeamRole | null>(null);
  const [versions, setVersions] = useState<OntologyVersionSummary[] | null>(
    null,
  );
  const [unpublished, setUnpublished] = useState<OntologyUnpublished | null>(
    null,
  );
  const [loading, setLoading] = useState(false);
  // 加载失败：文本走 apiErrorText（有后端 detail 用 detail，否则兜底），
  // status 单列供错误卡加 mono 状态码行（报因可读，不裸 403）。
  const [loadError, setLoadError] = useState<{
    text: string;
    status: number | null;
  } | null>(null);

  // 打发布版（label 可选；unchanged 时如实说明未新增版本）。
  const [publishOpen, setPublishOpen] = useState(false);
  const [label, setLabel] = useState("");
  const [publishing, setPublishing] = useState(false);
  const [publishResult, setPublishResult] =
    useState<OntologyPublishResult | null>(null);
  const [publishError, setPublishError] = useState<string | null>(null);

  // 导出（blob 经 a[download] 触发下载；失败如实）。
  const [exportError, setExportError] = useState<string | null>(null);

  // diff 面板（一次只展开一个版本）。
  const [diffFor, setDiffFor] = useState<number | null>(null);
  const [diff, setDiff] = useState<OntologyDiff | null>(null);
  const [diffLoading, setDiffLoading] = useState(false);
  const [diffError, setDiffError] = useState<string | null>(null);

  // 回滚（plan 预览 → owner 确认执行）。
  const [rollbackFor, setRollbackFor] = useState<number | null>(null);
  const [rollbackPlan, setRollbackPlan] =
    useState<OntologyRollbackPlan | null>(null);
  const [rollbackLoading, setRollbackLoading] = useState(false);
  const [rollbackError, setRollbackError] = useState<{
    message: string;
    executed: number | null;
    note: string | null;
  } | null>(null);
  const [rollbackBusy, setRollbackBusy] = useState(false);
  const [rollbackResult, setRollbackResult] =
    useState<OntologyRollbackResult | null>(null);

  // 完整性自检（一致=绿、不一致=红并展开差异）。
  const [integrity, setIntegrity] = useState<OntologyIntegrity | null>(null);
  const [integrityError, setIntegrityError] = useState<string | null>(null);
  const [integrityOpen, setIntegrityOpen] = useState(false);

  const isOwner = role === "owner";

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const data = await fetchOntologyVersions();
      setVersions(data.versions);
      setUnpublished(data.unpublished);
    } catch (err) {
      setVersions(null);
      setUnpublished(null);
      setLoadError({
        text: apiErrorText(err, t("console.entities.version.loadFailed")),
        status:
          err instanceof ConsoleApiError && typeof err.status === "number"
            ? err.status
            : null,
      });
    } finally {
      setLoading(false);
    }
  }, [t]);

  const loadIntegrity = useCallback(async () => {
    setIntegrityError(null);
    try {
      setIntegrity(await fetchOntologyIntegrity());
    } catch (err) {
      setIntegrity(null);
      setIntegrityError(
        apiErrorText(err, t("console.entities.version.integrityFailed")),
      );
    }
  }, [t]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    void loadIntegrity();
  }, [loadIntegrity]);

  // 角色只拉一次（与 DatasetAccessSection 同口径；失败即不可见）。
  useEffect(() => {
    let cancelled = false;
    getTeam()
      .then((team) => {
        if (!cancelled) setRole(team.me.role);
      })
      .catch(() => {
        if (!cancelled) setRole(null);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const unpublishedCount = unpublished?.count ?? 0;
  const integrityCount = integrity ? diffTotal(integrity.differences) : 0;

  const integrityBadgeText = useMemo(() => {
    if (integrity) {
      return integrity.consistent
        ? t("console.entities.version.integrityOk")
        : t("console.entities.version.integrityBad", {
            count: integrityCount,
          });
    }
    return integrityError
      ? t("console.entities.version.integrityUnavailable")
      : t("console.entities.version.integrity");
  }, [integrity, integrityError, integrityCount, t]);

  const submitPublish = async () => {
    if (publishing) return;
    setPublishing(true);
    setPublishError(null);
    setPublishResult(null);
    try {
      const result = await publishOntologyVersion(label);
      setPublishResult(result);
      setLabel("");
      setPublishOpen(false);
      await Promise.all([load(), loadIntegrity()]);
    } catch (err) {
      setPublishError(
        apiErrorText(err, t("console.entities.version.publishFailed")),
      );
    } finally {
      setPublishing(false);
    }
  };

  const runExport = async (
    version: number | "current",
    format: OntologyExportFormat,
  ) => {
    setExportError(null);
    try {
      const { blob, filename } = await downloadOntologyExport({
        version,
        format,
      });
      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(objectUrl);
    } catch (err) {
      setExportError(
        apiErrorText(err, t("console.entities.version.exportFailed")),
      );
    }
  };

  const toggleDiff = async (version: number) => {
    if (diffFor === version) {
      setDiffFor(null);
      setDiff(null);
      setDiffError(null);
      return;
    }
    setDiffFor(version);
    setDiff(null);
    setDiffError(null);
    setDiffLoading(true);
    try {
      setDiff(await fetchOntologyDiff(version, "current"));
    } catch (err) {
      setDiffError(
        apiErrorText(err, t("console.entities.version.diffFailed")),
      );
    } finally {
      setDiffLoading(false);
    }
  };

  const openRollback = async (version: number) => {
    if (rollbackFor === version) {
      setRollbackFor(null);
      setRollbackPlan(null);
      setRollbackError(null);
      setRollbackResult(null);
      return;
    }
    setRollbackFor(version);
    setRollbackPlan(null);
    setRollbackError(null);
    setRollbackResult(null);
    setRollbackLoading(true);
    try {
      setRollbackPlan(await fetchOntologyRollbackPlan(version));
    } catch (err) {
      setRollbackError(
        parseRollbackFailure(
          err,
          t("console.entities.version.rollbackPlanFailed"),
        ),
      );
    } finally {
      setRollbackLoading(false);
    }
  };

  const confirmRollback = async (version: number) => {
    if (rollbackBusy) return;
    setRollbackBusy(true);
    setRollbackError(null);
    setRollbackResult(null);
    try {
      const result = await executeOntologyRollback(version);
      setRollbackResult(result);
      await Promise.all([load(), loadIntegrity()]);
    } catch (err) {
      setRollbackError(
        parseRollbackFailure(
          err,
          t("console.entities.version.rollbackFailed"),
        ),
      );
    } finally {
      setRollbackBusy(false);
    }
  };

  const identityText = (items: unknown[]): string =>
    items
      .map((item) =>
        typeof item === "string" ? item : JSON.stringify(item),
      )
      .join("；");

  return (
    <section
      data-testid="ontology-version-section"
      className="mt-4 rounded-lg border border-slate-200 bg-white p-4"
    >
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold text-slate-800">
          {t("console.entities.version.title")}
        </h2>
        <span
          data-testid="ontology-unpublished-badge"
          data-count={unpublishedCount}
          className={`rounded-full px-2 py-0.5 text-[11px] ring-1 ${
            unpublishedCount > 0
              ? "bg-amber-50 text-amber-700 ring-amber-200"
              : "bg-slate-100 text-slate-500 ring-slate-200"
          }`}
        >
          {t("console.entities.version.unpublishedBadge", {
            count: unpublishedCount,
          })}
        </span>
        {unpublished?.since_version != null && (
          <span className="text-[11px] text-slate-400">
            {t("console.entities.version.unpublishedSince", {
              version: unpublished.since_version,
            })}
          </span>
        )}
        <div className="ml-auto flex flex-wrap gap-2">
          <button
            type="button"
            data-testid="ontology-publish"
            onClick={() => {
              setPublishOpen((open) => !open);
              setPublishError(null);
            }}
            className="rounded bg-slate-900 px-3 py-1 text-xs text-white hover:bg-slate-700"
          >
            {t("console.entities.version.publish")}
          </button>
          <button
            type="button"
            data-testid="ontology-export-json"
            onClick={() => void runExport("current", "json")}
            className="rounded border border-slate-300 px-2.5 py-1 text-xs text-slate-600 hover:bg-slate-50"
          >
            {t("console.entities.version.exportCurrentJson")}
          </button>
          <button
            type="button"
            data-testid="ontology-export-yaml"
            onClick={() => void runExport("current", "yaml")}
            className="rounded border border-slate-300 px-2.5 py-1 text-xs text-slate-600 hover:bg-slate-50"
          >
            {t("console.entities.version.exportCurrentYaml")}
          </button>
          <button
            type="button"
            data-testid="ontology-integrity-badge"
            data-consistent={
              integrity ? String(integrity.consistent) : "unknown"
            }
            onClick={() => setIntegrityOpen((open) => !open)}
            className={`rounded-full px-2.5 py-1 text-[11px] ring-1 ${
              integrity == null
                ? "bg-slate-100 text-slate-500 ring-slate-200"
                : integrity.consistent
                  ? "bg-emerald-50 text-emerald-700 ring-emerald-200"
                  : "bg-rose-50 text-rose-700 ring-rose-200"
            }`}
          >
            {integrityBadgeText}
          </button>
        </div>
      </div>
      <p className="mt-1 max-w-3xl text-xs text-slate-500">
        {t("console.entities.version.subtitle")}
      </p>

      {publishOpen && (
        <form
          data-testid="ontology-publish-form"
          onSubmit={(event) => {
            event.preventDefault();
            void submitPublish();
          }}
          className="mt-3 rounded border border-slate-200 bg-slate-50 p-3"
        >
          <label
            htmlFor="ontology-publish-label"
            className="block text-xs text-slate-600"
          >
            {t("console.entities.version.publishLabelLabel")}
          </label>
          <div className="mt-1 flex flex-wrap gap-2">
            <input
              id="ontology-publish-label"
              data-testid="ontology-publish-label"
              value={label}
              maxLength={80}
              onChange={(event) => setLabel(event.target.value)}
              placeholder={t(
                "console.entities.version.publishLabelPlaceholder",
              )}
              className="min-w-64 flex-1 rounded border border-slate-300 px-2 py-1 text-xs text-slate-700"
            />
            <button
              type="submit"
              data-testid="ontology-publish-submit"
              disabled={publishing}
              className="rounded bg-slate-900 px-3 py-1 text-xs text-white hover:bg-slate-700 disabled:opacity-50"
            >
              {publishing
                ? t("console.entities.version.publishing")
                : t("console.entities.version.publishSubmit")}
            </button>
            <button
              type="button"
              data-testid="ontology-publish-cancel"
              onClick={() => {
                setPublishOpen(false);
                setPublishError(null);
              }}
              className="rounded border border-slate-300 px-2.5 py-1 text-xs text-slate-600 hover:bg-slate-100"
            >
              {t("console.entities.version.publishCancel")}
            </button>
          </div>
        </form>
      )}

      {publishResult && (
        <p
          data-testid="ontology-publish-result"
          data-unchanged={String(publishResult.unchanged)}
          className={`mt-3 rounded border px-3 py-2 text-xs ${
            publishResult.unchanged
              ? "border-amber-200 bg-amber-50 text-amber-800"
              : "border-emerald-200 bg-emerald-50 text-emerald-700"
          }`}
        >
          {publishResult.unchanged
            ? t("console.entities.version.publishUnchanged", {
                message:
                  publishResult.message ??
                  t("console.entities.version.publishUnchangedFallback", {
                    version: publishResult.version,
                  }),
              })
            : t("console.entities.version.publishOk", {
                version: publishResult.version,
              })}
        </p>
      )}
      {publishError && (
        <p
          data-testid="ontology-publish-error"
          className="mt-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700"
        >
          {publishError}
        </p>
      )}
      {exportError && (
        <p
          data-testid="ontology-export-error"
          className="mt-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700"
        >
          {exportError}
        </p>
      )}

      {integrityOpen && (
        <div
          data-testid="ontology-integrity-panel"
          className="mt-3 rounded border border-slate-200 bg-slate-50 p-3"
        >
          {integrityError ? (
            <p
              data-testid="ontology-integrity-error"
              className="text-xs text-red-700"
            >
              {integrityError}
            </p>
          ) : integrity ? (
            <>
              <p
                data-testid="ontology-integrity-summary"
                className={`text-xs ${
                  integrity.consistent ? "text-emerald-700" : "text-rose-700"
                }`}
              >
                {integrity.consistent
                  ? t("console.entities.version.integrityOk")
                  : t("console.entities.version.integrityBad", {
                      count: integrityCount,
                    })}
              </p>
              <p className="mt-1 text-[11px] text-slate-500">
                {t("console.entities.version.integrityBaseline", {
                  kind:
                    integrity.baseline.kind === "snapshot"
                      ? t("console.entities.version.baselineSnapshot")
                      : t("console.entities.version.baselineEmpty"),
                  events: integrity.baseline.events_folded,
                })}
              </p>
              {!integrity.consistent && (
                <>
                  {integrity.escape_hint && (
                    <p
                      data-testid="ontology-integrity-hint"
                      className="mt-1 text-[11px] text-rose-600"
                    >
                      {integrity.escape_hint}
                    </p>
                  )}
                  <div className="mt-2 space-y-2 overflow-x-auto">
                    <DiffGroupView
                      testId="ontology-integrity-entities"
                      titleKey="console.entities.version.entityGroup"
                      group={integrity.differences.entities}
                      itemLabel={entityLabel}
                      changeLabel={(change) => entityLabel(change.after)}
                    />
                    <DiffGroupView
                      testId="ontology-integrity-relationships"
                      titleKey="console.entities.version.relationshipGroup"
                      group={integrity.differences.relationships}
                      itemLabel={relationshipLabel}
                      itemMono
                      changeLabel={(change) =>
                        relationshipLabel(change.after)
                      }
                    />
                  </div>
                </>
              )}
            </>
          ) : (
            <p className="text-xs text-slate-400">
              {t("console.entities.version.integrityLoading")}
            </p>
          )}
        </div>
      )}

      {loadError && (
        <div
          data-testid="ontology-version-error"
          className="mt-3 rounded-md border border-red-200 bg-red-50 p-3"
        >
          <p className="text-xs text-red-700">{loadError.text}</p>
          {loadError.status != null && (
            <p
              data-testid="ontology-version-error-status"
              className="mt-1 font-mono text-[11px] text-red-500"
            >
              HTTP {loadError.status}
            </p>
          )}
          <button
            type="button"
            data-testid="ontology-version-retry"
            onClick={() => void load()}
            className="mt-2 rounded border border-red-300 px-2 py-0.5 text-xs text-red-700 hover:bg-red-100"
          >
            {t("console.entities.version.retry")}
          </button>
        </div>
      )}

      {versions == null && loading && (
        <p
          data-testid="ontology-version-loading"
          className="mt-3 flex min-h-24 items-center text-xs text-slate-400"
        >
          {t("console.entities.version.loading")}
        </p>
      )}

      {versions != null && versions.length === 0 && (
        <div className="mt-3 rounded-lg border border-dashed border-slate-300 p-8 text-center">
          <p
            data-testid="ontology-version-empty"
            className="text-sm text-slate-600"
          >
            {t("console.entities.version.empty")}
          </p>
          <p className="mt-1 text-xs text-slate-400">
            {t("console.entities.version.emptyHint")}
          </p>
        </div>
      )}

      {versions != null && versions.length > 0 && (
        <ul data-testid="ontology-version-timeline" className="mt-3 space-y-2">
          {versions.map((version) => (
            <li
              key={version.version}
              data-testid="ontology-version-row"
              data-version={version.version}
              className="rounded border border-slate-200 bg-slate-50 p-3"
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-xs font-semibold text-slate-800">
                  v{version.version}
                </span>
                <code
                  data-testid="ontology-version-digest"
                  title={version.digest}
                  className="rounded bg-white px-1.5 py-0.5 font-mono text-[11px] text-slate-600 ring-1 ring-slate-200"
                >
                  {version.digest.slice(0, 8)}
                </code>
                <span
                  data-testid="ontology-version-trigger"
                  data-trigger={version.trigger}
                  className={`rounded-full px-2 py-0.5 text-[11px] ring-1 ${
                    version.trigger === "manual"
                      ? "bg-indigo-50 text-indigo-700 ring-indigo-200"
                      : "bg-slate-100 text-slate-600 ring-slate-200"
                  }`}
                >
                  {t(`console.entities.version.trigger.${version.trigger}`)}
                </span>
                {version.label && (
                  <span
                    data-testid="ontology-version-label"
                    className="text-xs text-slate-700"
                  >
                    {version.label}
                  </span>
                )}
                <span
                  data-testid="ontology-version-counts"
                  className="text-[11px] text-slate-500"
                >
                  {t("console.entities.version.counts", {
                    entities: version.entity_count,
                    relations: version.relationship_count,
                  })}
                </span>
                <span
                  data-testid="ontology-version-time"
                  className="text-[11px] text-slate-400"
                >
                  {formatTime(version.created_at)}
                </span>
                <div className="ml-auto flex flex-wrap gap-2">
                  <button
                    type="button"
                    data-testid="ontology-diff-open"
                    data-version={version.version}
                    onClick={() => void toggleDiff(version.version)}
                    className="rounded border border-slate-300 px-2.5 py-1 text-xs text-slate-600 hover:bg-white"
                  >
                    {t("console.entities.version.compareCurrent")}
                  </button>
                  <button
                    type="button"
                    data-testid="ontology-version-export"
                    data-version={version.version}
                    onClick={() => void runExport(version.version, "json")}
                    className="rounded border border-slate-300 px-2.5 py-1 text-xs text-slate-600 hover:bg-white"
                  >
                    {t("console.entities.version.exportVersion")}
                  </button>
                  {isOwner && (
                    <button
                      type="button"
                      data-testid="ontology-rollback-open"
                      data-version={version.version}
                      onClick={() => void openRollback(version.version)}
                      className="rounded border border-rose-300 px-2.5 py-1 text-xs text-rose-700 hover:bg-rose-50"
                    >
                      {t("console.entities.version.rollback")}
                    </button>
                  )}
                </div>
              </div>

              {diffFor === version.version && (
                <div
                  data-testid="ontology-diff-panel"
                  data-version={version.version}
                  className="mt-3 rounded border border-slate-200 bg-white p-3"
                >
                  <div className="flex items-center justify-between gap-2">
                    <p className="text-xs font-medium text-slate-700">
                      {t("console.entities.version.diffTitle", {
                        version: version.version,
                      })}
                    </p>
                    <button
                      type="button"
                      data-testid="ontology-diff-close"
                      onClick={() => void toggleDiff(version.version)}
                      className="rounded border border-slate-300 px-2 py-0.5 text-[11px] text-slate-500 hover:bg-slate-50"
                    >
                      {t("console.entities.version.diffClose")}
                    </button>
                  </div>
                  {diffLoading && (
                    <p className="mt-2 text-xs text-slate-400">
                      {t("console.entities.version.diffLoading")}
                    </p>
                  )}
                  {diffError && (
                    <p
                      data-testid="ontology-diff-error"
                      className="mt-2 text-xs text-red-700"
                    >
                      {diffError}
                    </p>
                  )}
                  {diff && !diff.has_changes && (
                    <p
                      data-testid="ontology-diff-empty"
                      className="mt-2 rounded border border-amber-200 bg-amber-50 px-2 py-1 text-xs text-amber-800"
                    >
                      {t("console.entities.version.noDiff")}
                    </p>
                  )}
                  {diff && diff.has_changes && (
                    <div className="mt-2 space-y-2 overflow-x-auto">
                      <DiffGroupView
                        testId="ontology-diff-entities"
                        titleKey="console.entities.version.entityGroup"
                        group={diff.entities}
                        itemLabel={entityLabel}
                        changeLabel={(change) => entityLabel(change.after)}
                      />
                      <DiffGroupView
                        testId="ontology-diff-relationships"
                        titleKey="console.entities.version.relationshipGroup"
                        group={diff.relationships}
                        itemLabel={relationshipLabel}
                        itemMono
                        changeLabel={(change) =>
                          relationshipLabel(change.after)
                        }
                      />
                    </div>
                  )}
                </div>
              )}

              {rollbackFor === version.version && (
                <div
                  data-testid="ontology-rollback-plan"
                  data-version={version.version}
                  className="mt-3 rounded border border-rose-200 bg-white p-3"
                >
                  <p className="text-xs font-medium text-slate-700">
                    {t("console.entities.version.rollbackPlanTitle", {
                      version: version.version,
                    })}
                  </p>
                  {rollbackPlan && !rollbackPlan.empty && (
                    <p className="mt-1 text-[11px] text-rose-600">
                      {t("console.entities.version.rollbackWarning", {
                        version: version.version,
                      })}
                    </p>
                  )}
                  {rollbackLoading && (
                    <p className="mt-2 text-xs text-slate-400">
                      {t("console.entities.version.rollbackWorking")}
                    </p>
                  )}
                  {rollbackError && (
                    <div
                      data-testid="ontology-rollback-error"
                      className="mt-2 rounded border border-red-200 bg-red-50 px-2 py-1.5 text-xs text-red-700"
                    >
                      <p>{rollbackError.message}</p>
                      {rollbackError.executed != null && (
                        <p
                          data-testid="ontology-rollback-error-executed"
                          className="mt-1 text-[11px] text-red-600"
                        >
                          {t("console.entities.version.rollbackExecuted", {
                            count: rollbackError.executed,
                          })}
                        </p>
                      )}
                      {rollbackError.note && (
                        <p className="mt-1 text-[11px] text-red-600">
                          {rollbackError.note}
                        </p>
                      )}
                    </div>
                  )}
                  {rollbackPlan && (
                    <>
                      {rollbackPlan.empty ? (
                        <p
                          data-testid="ontology-rollback-empty"
                          className="mt-2 text-xs text-slate-500"
                        >
                          {t(
                            "console.entities.version.rollbackPlanEmpty",
                            { version: version.version },
                          )}
                        </p>
                      ) : (
                        <>
                          <ul
                            data-testid="ontology-rollback-summary"
                            className="mt-2 space-y-0.5 text-[11px] text-slate-600"
                          >
                            <li>
                              {t(
                                "console.entities.version.rollbackEntities",
                                {
                                  create:
                                    rollbackPlan.plan.summary.entities.create,
                                  update:
                                    rollbackPlan.plan.summary.entities.update,
                                  delete:
                                    rollbackPlan.plan.summary.entities.delete,
                                },
                              )}
                            </li>
                            <li>
                              {t(
                                "console.entities.version.rollbackRelationships",
                                {
                                  create:
                                    rollbackPlan.plan.summary.relationships
                                      .create,
                                  delete:
                                    rollbackPlan.plan.summary.relationships
                                      .delete,
                                },
                              )}
                            </li>
                          </ul>
                          {rollbackPlan.plan.update_entities.length > 0 && (
                            <div className="overflow-x-auto">
                              <ul
                                data-testid="ontology-rollback-updates"
                                className="mt-2 min-w-max space-y-1 text-[11px] text-slate-600"
                              >
                                {rollbackPlan.plan.update_entities.map(
                                  (update) => (
                                    <li
                                      key={`${update.id ?? update.name}`}
                                      data-testid="ontology-rollback-update"
                                    >
                                      <span className="font-mono">
                                        {update.name} (#{update.id})
                                      </span>
                                      <ul className="ml-3 space-y-0.5">
                                        {Object.entries(update.fields).map(
                                          ([field, delta]) => (
                                            <li
                                              key={field}
                                              data-testid="ontology-rollback-field"
                                              data-field={field}
                                              className="font-mono text-slate-500"
                                            >
                                              {field}:{" "}
                                              {fieldText(delta.before)} →{" "}
                                              {fieldText(delta.after)}
                                            </li>
                                          ),
                                        )}
                                      </ul>
                                    </li>
                                  ),
                                )}
                              </ul>
                            </div>
                          )}
                          <div className="mt-2 flex flex-wrap gap-2">
                            {isOwner && (
                              <button
                                type="button"
                                data-testid="ontology-rollback-confirm"
                                disabled={rollbackBusy}
                                onClick={() =>
                                  void confirmRollback(version.version)
                                }
                                className="rounded bg-rose-600 px-3 py-1 text-xs text-white hover:bg-rose-500 disabled:opacity-50"
                              >
                                {rollbackBusy
                                  ? t(
                                      "console.entities.version.rollbackWorking",
                                    )
                                  : t(
                                      "console.entities.version.rollbackConfirm",
                                    )}
                              </button>
                            )}
                            <button
                              type="button"
                              data-testid="ontology-rollback-cancel"
                              onClick={() => void openRollback(version.version)}
                              className="rounded border border-slate-300 px-2.5 py-1 text-xs text-slate-600 hover:bg-slate-50"
                            >
                              {t("console.entities.version.rollbackCancel")}
                            </button>
                          </div>
                        </>
                      )}
                    </>
                  )}
                  {rollbackResult && (
                    <div
                      data-testid="ontology-rollback-result"
                      data-digest-match={String(rollbackResult.digest_match)}
                      className={`mt-2 rounded border px-2 py-1.5 text-xs ${
                        rollbackResult.digest_match
                          ? "border-emerald-200 bg-emerald-50 text-emerald-700"
                          : "border-amber-200 bg-amber-50 text-amber-800"
                      }`}
                    >
                      <p data-testid="ontology-rollback-result-message">
                        {rollbackResult.message ??
                          (rollbackResult.digest_match
                            ? t(
                                "console.entities.version.rollbackDigestMatch",
                              )
                            : t(
                                "console.entities.version.rollbackDigestMismatch",
                              ))}
                      </p>
                      <p className="mt-1 text-[11px]">
                        {t("console.entities.version.rollbackExecuted", {
                          count: rollbackResult.executed.length,
                        })}
                      </p>
                      {rollbackResult.identity_changed.length > 0 && (
                        <p
                          data-testid="ontology-rollback-identity"
                          className="mt-1 text-[11px]"
                        >
                          {t("console.entities.version.rollbackIdentity", {
                            items: identityText(
                              rollbackResult.identity_changed,
                            ),
                          })}
                        </p>
                      )}
                    </div>
                  )}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
