// 实体对象浏览器（wire-entity-v2）：实例列表（q 检索+分页）→ 对象详情
// （属性按列注记渲染 + 沿关系遍历的关联对象）。只读面——对象修改沿修正层
// 与动作闸；cell 档专属（免费档 403 由后端如实返回，前端如实呈现错误）。
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  apiErrorText,
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

export function EntityObjectBrowser({ entity, edges }: Props) {
  const { t } = useTranslation();
  const [q, setQ] = useState("");
  const [, setPage] = useState(1);
  const [list, setList] = useState<EntityObjectPage | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [detail, setDetail] = useState<EntityObjectDetail | null>(null);
  const [relId, setRelId] = useState<number | null>(null);
  const [relPage, setRelPage] = useState<EntityRelationPage | null>(null);
  const [relLoading, setRelLoading] = useState(false);

  const pkColumn = entity.pk_columns[0] ?? "id";
  const displayColumn = entity.display_column ?? pkColumn;

  const loadList = useCallback(
    async (nextPage: number) => {
      setLoading(true);
      setError(null);
      try {
        const data = await listEntityObjects(entity.id, {
          q: q.trim() || undefined,
          page: nextPage,
          page_size: 10,
        });
        setList(data);
        setPage(nextPage);
      } catch (err) {
        setError(apiErrorText(err, t("console.entities.objects.loadFailed")));
      } finally {
        setLoading(false);
      }
    },
    [entity.id, q, t],
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
        setError(apiErrorText(err, t("console.entities.objects.loadFailed")));
      }
    },
    [entity.id, t],
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
        setError(apiErrorText(err, t("console.entities.objects.loadFailed")));
      } finally {
        setRelLoading(false);
      }
    },
    [entity.id, selectedKey, t],
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
      <div className="flex items-center gap-2">
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
          className="w-56 rounded-md border border-slate-300 px-2 py-1 text-sm"
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
          {list?.total != null
            ? t("console.entities.objects.total", { count: list.total })
            : null}
        </span>
      </div>
      {error && (
        <p data-testid="entity-objects-error" className="mt-2 text-xs text-rose-700">
          {error}
        </p>
      )}
      {list && (
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
                  className={`w-full rounded px-2 py-1 text-left text-sm hover:bg-slate-50 ${
                    selectedKey === key ? "bg-indigo-50" : ""
                  }`}
                >
                  {renderValue(displayColumn, obj[displayColumn])}
                  <span className="ml-2 font-mono text-[11px] text-slate-400">
                    {key}
                  </span>
                </button>
              </li>
            );
          })}
          {list.objects.length === 0 && (
            <li className="text-xs text-slate-400">
              {t("console.entities.objects.empty")}
            </li>
          )}
        </ul>
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
      {detail?.found && detail.object && (
        <div
          data-testid="entity-object-detail"
          className="mt-3 rounded border border-slate-200 bg-slate-50/60 p-3"
        >
          <p className="text-xs font-semibold text-slate-700">
            {t("console.entities.objects.detailTitle")}
          </p>
          <dl className="mt-1 grid grid-cols-[max-content_1fr] gap-x-3 gap-y-0.5 text-xs">
            {Object.entries(detail.object).map(([column, value]) => (
              <div key={column} className="col-span-2 grid grid-cols-subgrid">
                <dt className="font-mono text-slate-500">{column}</dt>
                <dd className="text-slate-700">
                  {renderValue(column, value)}
                </dd>
              </div>
            ))}
          </dl>
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
                className="w-64 rounded-md border border-slate-300 px-2 py-1 text-xs"
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
                        className="rounded bg-white px-2 py-1 text-xs text-slate-700 ring-1 ring-slate-100"
                      >
                        {relPage.dataset_name} · {key}
                      </li>
                    );
                  })}
                  {relPage.objects.length === 0 && (
                    <li className="text-xs text-slate-400">
                      {t("console.entities.objects.empty")}
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
