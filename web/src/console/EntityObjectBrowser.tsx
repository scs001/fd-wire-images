// 实体对象浏览器（wire-entity-v2）：实例列表（q 检索+分页）→ 对象详情
// （属性按列注记渲染 + 沿关系遍历的关联对象）。只读面——对象修改沿修正层
// 与动作闸；cell 档专属（免费档 403 由后端如实返回，前端按 API 已返回的
// 错误类别做分型映射呈现，不重判 ACL）。
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  apiErrorText,
  ConsoleApiError,
  EntityItem,
  EntityObjectDetail,
  EntityObjectPage,
  EntityRelationPage,
  EntityRelationship,
  getEntityObject,
  listEntityObjectRelations,
  listEntityObjects,
} from "../console/api";

interface Props {
  entity: EntityItem;
  /** 与本实体相关的图谱边（关系遍历下拉；声明方向呈现）。 */
  edges: EntityRelationship[];
}

// ---------------------------------------------------------------------------
// ACL 403 分型（组件内局部纯函数——brief D3：呈现层只做映射显示）。
// 判定只看 ConsoleApiError.status===403 与 detail 形态（API 已返回类别），
// 不重发请求、不重判 ACL：
//   - tier-gate：detail 为对象且 code=PLAN_TIER_REQUIRED
//     （datasets/routes.py plan_tier_error：{code,message,upgrade}）。
//   - read-denied：detail 为字符串且含「受限数据集」（datasets/acl.py；
//     对象浏览为读路径，受限集拒绝即读授权缺失）。
//   - unknown-403：其余 403（如实透传 detail 原文，不猜型）。
// 非 403 / 非 ConsoleApiError → null（走既有 apiErrorText 呈现）。
// ---------------------------------------------------------------------------
type AclKind = "tier-gate" | "read-denied" | "unknown-403";
type ErrorState = { kind: AclKind | "plain"; message: string } | null;

function classifyBrowseError(err: unknown): AclKind | null {
  if (!(err instanceof ConsoleApiError) || err.status !== 403) return null;
  const detail = err.detail;
  if (detail && typeof detail === "object") {
    const code = (detail as Record<string, unknown>).code;
    if (
      typeof code === "string" &&
      code.toUpperCase() === "PLAN_TIER_REQUIRED"
    ) {
      return "tier-gate";
    }
    return "unknown-403";
  }
  if (typeof detail === "string" && detail.includes("受限数据集")) {
    return "read-denied";
  }
  return "unknown-403";
}

export function EntityObjectBrowser({ entity, edges }: Props) {
  const { t } = useTranslation();
  const [q, setQ] = useState("");
  const [, setPage] = useState(1);
  const [list, setList] = useState<EntityObjectPage | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<ErrorState>(null);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [detail, setDetail] = useState<EntityObjectDetail | null>(null);
  const [relId, setRelId] = useState<number | null>(null);
  const [relPage, setRelPage] = useState<EntityRelationPage | null>(null);
  const [relLoading, setRelLoading] = useState(false);

  const pkColumn = entity.pk_columns[0] ?? "id";
  const displayColumn = entity.display_column ?? pkColumn;

  // 错误归一：403 分型映射（brief 三型文案），其余走既有 apiErrorText。
  const toErrorState = useCallback(
    (err: unknown): ErrorState => {
      const kind = classifyBrowseError(err);
      if (kind === "tier-gate") {
        return {
          kind,
          message: t("console.entities.objects.aclTierGateMessage"),
        };
      }
      if (kind === "read-denied") {
        return {
          kind,
          message: t("console.entities.objects.aclReadDeniedMessage"),
        };
      }
      return {
        kind: kind === "unknown-403" ? kind : "plain",
        message: apiErrorText(err, t("console.entities.objects.loadFailed")),
      };
    },
    [t]
  );

  const loadList = useCallback(
    async (nextPage: number, nextQ?: string) => {
      const query = (nextQ ?? q).trim();
      setLoading(true);
      setError(null);
      try {
        const data = await listEntityObjects(entity.id, {
          q: query || undefined,
          page: nextPage,
          page_size: 10,
        });
        setList(data);
        setPage(nextPage);
      } catch (err) {
        setError(toErrorState(err));
      } finally {
        setLoading(false);
      }
    },
    [entity.id, q, toErrorState],
  );

  useEffect(() => {
    void loadList(1);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- q 变化由搜索按钮触发
  }, [entity.id]);

  const openObject = useCallback(
    async (key: string) => {
      setSelectedKey(key);
      setDetail(null);
      setRelId(null);
      setRelPage(null);
      try {
        setDetail(await getEntityObject(entity.id, key));
      } catch (err) {
        setError(toErrorState(err));
      }
    },
    [entity.id, toErrorState],
  );

  const loadRelations = useCallback(
    async (relationshipId: number, nextPage: number) => {
      if (!selectedKey) return;
      setRelLoading(true);
      try {
        setRelPage(
          await listEntityObjectRelations(
            entity.id,
            selectedKey,
            relationshipId,
            nextPage,
          ),
        );
      } catch (err) {
        setError(toErrorState(err));
      } finally {
        setRelLoading(false);
      }
    },
    [entity.id, selectedKey, toErrorState],
  );

  const renderValue = (column: string, value: unknown): string => {
    const note = entity.column_notes?.[column];
    if (note && value != null && value !== "") return `${value}（${note}）`;
    return value == null ? "—" : String(value);
  };

  return (
    <div
      data-testid="entity-object-browser"
      data-entity-id={entity.id}
      className="mt-3 rounded-md border border-slate-200 bg-white p-3"
    >
      <div className="flex flex-wrap items-center gap-2">
        <input
          data-testid="entity-objects-q"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void loadList(1);
          }}
          placeholder={t("console.entities.objects.searchPlaceholder", {
            column: displayColumn,
          })}
          className="w-56 max-w-full rounded-md border border-slate-300 px-2 py-1 text-sm"
        />
        <button
          type="button"
          data-testid="entity-objects-search"
          onClick={() => void loadList(1)}
          className="rounded border border-slate-300 px-2.5 py-1 text-xs text-slate-600 hover:bg-slate-50"
        >
          {t("console.entities.objects.search")}
        </button>
        <span className="text-[11px] text-slate-400">
          {loading && list
            ? t("console.entities.objects.loading")
            : list?.total != null
              ? t("console.entities.objects.total", { count: list.total })
              : null}
        </span>
      </div>
      {error && (
        <div
          data-testid="entity-objects-error"
          className="mt-2 rounded border border-red-200 bg-red-50 px-3 py-2"
        >
          {/* unknown-403 为 detail 原文透传——机器细节用 mono；其余为映射文案。 */}
          <p
            className={`text-xs text-red-700 ${
              error.kind === "unknown-403" ? "font-mono" : ""
            }`}
          >
            {error.message}
          </p>
          {error.kind === "tier-gate" && (
            <p className="mt-1 text-xs text-red-600">
              {t("console.entities.objects.aclTierGateNext")}
            </p>
          )}
          {error.kind === "read-denied" && (
            <p className="mt-1 text-xs text-red-600">
              {t("console.entities.objects.aclReadDeniedNext")}
            </p>
          )}
          {error.kind === "unknown-403" && (
            <p className="mt-1 text-xs text-red-600">
              {t("console.entities.objects.aclUnknownNext")}
            </p>
          )}
        </div>
      )}
      {/* 首次加载占位：固定 min-height text-line，内容现处不闪跳、不建骨架。 */}
      {loading && !list && (
        <p
          data-testid="entity-objects-loading"
          className="mt-2 min-h-[8rem] text-xs text-slate-400"
        >
          {t("console.entities.objects.loading")}
        </p>
      )}
      {list && list.objects.length > 0 && (
        <ul data-testid="entity-objects-list" className="mt-2 space-y-1">
          {list.objects.map((obj) => {
            const key = String(obj[pkColumn] ?? "");
            return (
              <li key={key}>
                <button
                  type="button"
                  data-testid="entity-object-row"
                  data-key={key}
                  onClick={() => void openObject(key)}
                  className={`w-full rounded px-2 py-1 text-left text-sm font-mono [overflow-wrap:anywhere] hover:bg-slate-50 ${
                    selectedKey === key ? "bg-indigo-50" : ""
                  }`}
                >
                  {renderValue(displayColumn, obj[displayColumn])}
                  <span className="ml-2 text-[11px] text-slate-400">{key}</span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
      {list && list.objects.length === 0 && (
        <div
          data-testid="entity-objects-empty"
          className="mt-3 rounded-lg border border-dashed border-slate-300 p-8 text-center"
        >
          {q.trim() ? (
            <>
              {/* 筛选无结果：如实 + 重置 CTA（datasets assets-filtered-empty 同款）。 */}
              <p className="text-sm text-slate-600">
                {t("console.entities.objects.filteredEmpty")}
              </p>
              <button
                type="button"
                data-testid="entity-objects-clear"
                onClick={() => {
                  setQ("");
                  void loadList(1, "");
                }}
                className="mt-3 rounded border border-slate-300 px-3 py-1 text-xs text-slate-600 hover:bg-slate-50"
              >
                {t("console.entities.objects.filteredEmptyReset")}
              </button>
            </>
          ) : (
            <>
              {/* 真空态：0 对象行如实说明 + 来源引导（不摆拍示例对象）。 */}
              <p className="text-sm text-slate-600">
                {t("console.entities.objects.emptyTitle", {
                  name: entity.name,
                })}
              </p>
              <p className="mt-1 text-xs text-slate-400">
                {t("console.entities.objects.emptyHint")}
              </p>
            </>
          )}
        </div>
      )}
      {list && (list.page > 1 || list.objects.length === (list.page_size ?? 10)) && (
        <div className="mt-2 flex gap-2">
          <button
            type="button"
            disabled={list.page <= 1 || loading}
            onClick={() => void loadList(list.page - 1)}
            className="rounded border border-slate-300 px-2 py-0.5 text-xs disabled:opacity-40"
          >
            ←
          </button>
          <button
            type="button"
            disabled={loading}
            onClick={() => void loadList(list.page + 1)}
            className="rounded border border-slate-300 px-2 py-0.5 text-xs disabled:opacity-40"
          >
            →
          </button>
        </div>
      )}
      {selectedKey && !detail && !error && (
        <p className="mt-3 min-h-[5rem] text-xs text-slate-400">
          {t("console.entities.objects.loading")}
        </p>
      )}
      {detail?.found && detail.object && (
        <div
          data-testid="entity-object-detail"
          className="mt-3 rounded border border-slate-200 bg-slate-50/60 p-3"
        >
          <p className="text-xs font-semibold text-slate-700">
            {t("console.entities.objects.detailTitle")}
          </p>
          {/* 窄屏：值列优先收缩换行；超宽列名横向滚动兜底，不破卡片。 */}
          <div className="mt-1 overflow-x-auto">
            <dl className="grid grid-cols-[minmax(4rem,max-content)_minmax(0,1fr)] gap-x-3 gap-y-0.5 text-xs">
              {Object.entries(detail.object).map(([column, value]) => (
                <div key={column} className="col-span-2 grid grid-cols-subgrid">
                  <dt className="font-mono text-slate-500 [overflow-wrap:anywhere]">
                    {column}
                  </dt>
                  <dd className="min-w-0 font-mono text-slate-700 [overflow-wrap:anywhere]">
                    {renderValue(column, value)}
                  </dd>
                </div>
              ))}
            </dl>
          </div>
          {edges.length > 0 && (
            <div className="mt-3">
              <select
                data-testid="entity-object-relation-select"
                value={relId ?? ""}
                onChange={(e) => {
                  const id = Number(e.target.value);
                  setRelId(id || null);
                  setRelPage(null);
                  if (id) void loadRelations(id, 1);
                }}
                className="w-64 max-w-full rounded-md border border-slate-300 px-2 py-1 text-xs"
              >
                <option value="">
                  {t("console.entities.objects.relationPlaceholder")}
                </option>
                {edges.map((edge) => (
                  <option key={edge.id} value={edge.id}>
                    {`${edge.kind} · ${edge.from_entity_id === entity.id ? "→" : "←"} ${t(
                      "console.entities.objects.relationEdge",
                      { id: edge.from_entity_id === entity.id ? edge.to_entity_id : edge.from_entity_id },
                    )}`}
                  </option>
                ))}
              </select>
              {relLoading && (
                <p className="mt-1 text-[11px] text-slate-400">
                  {t("console.entities.objects.loading")}
                </p>
              )}
              {relPage && (
                <ul
                  data-testid="entity-object-relations"
                  className="mt-2 space-y-1"
                >
                  {relPage.objects.map((obj, index) => {
                    const key = String(
                      obj[(entity.pk_columns[0] ?? "id") as string] ?? index,
                    );
                    return (
                      <li
                        key={key}
                        className="rounded bg-white px-2 py-1 font-mono text-xs text-slate-700 ring-1 ring-slate-100 [overflow-wrap:anywhere]"
                      >
                        {relPage.dataset_name} · {key}
                      </li>
                    );
                  })}
                  {relPage.objects.length === 0 && (
                    <li className="rounded border border-dashed border-slate-300 px-2 py-3 text-center">
                      <p className="text-xs text-slate-600">
                        {t("console.entities.objects.relationEmpty")}
                      </p>
                      <p className="mt-1 text-[11px] text-slate-400">
                        {t("console.entities.objects.relationEmptyHint")}
                      </p>
                    </li>
                  )}
                </ul>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
