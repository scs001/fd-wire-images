// 控制台「实体」页（#/console/entities，wire-entity-layer 任务 3.2）。
// 轻量实体语义层 v0 的管理面，三区：
// - 实体卡列表：业务名 / 数据集名 + tier 徽标 / 主键列徽标组 / 描述 / 删除
//   （删除确认文案带级联关系数——计数来自图谱端点边）；
// - 创建表单：数据集下拉（本企业列表，显示行数）→ 主键列勾选（该集
//   columns 多选，至少一列）→ 业务名 → 描述；422 逐条 / 409 / 404 如实呈现；
// - 关系管理：每实体卡展开——关系行（from_column →(kind) 目标名.to_column
//   + 删除 + 「物化连接」[wire-entity-link：仅两端数据集均 cell 档渲染——
//   LEFT JOIN 一键生成连接数据集，成功提示新数据集名并可跳「我的数据」] 与
//   添加表单：目标实体下拉=其他实体、源列=本集列、**目标列下拉=目标实体
//   主键列（关系指向身份的纪律）**、kind n:1/1:1、label 可选；
// - SVG 小图谱：节点=实体（圆角矩形含业务名）、边=关系（线 + kind 小标注），
//   环形布局 v0 轻实现，节点点击滚动到对应实体卡；空态如实文案。
// 关系行数据源 = 图谱边（图谱是控制台页与 AI 上下文注入的唯一同源）；
// 数据集列表复用 fetchDatasets（columns 为主键勾选/源列下拉源）。免费档
// 可用（零引擎调用）。
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { DomainActionsPanel } from "./DomainActionsPanel";
import { EntityObjectBrowser } from "./EntityObjectBrowser";
import {
  ConsoleApiError,
  ENTITY_RELATIONSHIP_KINDS,
  apiErrorText,
  createEntity,
  createEntityRelationship,
  deleteEntity,
  deleteEntityRelationship,
  fetchDatasets,
  fetchEntities,
  fetchEntityGraph,
  materializeEntityRelationship,
  type Dataset,
  type EntityGraph,
  type EntityItem,
  type EntityRelationship,
  type EntityRelationshipKind,
} from "./api";

/** 错误文案行（如实逐条）：422 detailItems 数组逐条取 msg、string detail
 *  单行、其余兜底文案——绝不静默吞掉后端口径。 */
export function entityErrorLines(err: unknown, fallback: string): string[] {
  if (err instanceof ConsoleApiError) {
    if (err.detailItems && err.detailItems.length > 0) {
      return err.detailItems.map(
        (item, i) => item.msg ?? `${item.type ?? "invalid"} #${i + 1}`,
      );
    }
    if (err.detail) return [err.detail];
  }
  return [fallback];
}

/** 实体为其端点的关系（关系行数据源 = 图谱边）。 */
export function incidentRelationships(
  entityId: number,
  edges: ReadonlyArray<EntityRelationship>,
): EntityRelationship[] {
  return edges.filter(
    (edge) => edge.from_entity_id === entityId || edge.to_entity_id === entityId,
  );
}

export function EntitiesPage() {
  const { t } = useTranslation();
  const [entities, setEntities] = useState<EntityItem[] | null>(null);
  const [datasets, setDatasets] = useState<Dataset[] | null>(null);
  const [graph, setGraph] = useState<EntityGraph | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  // 展开关系面板的实体 id / 打开删除确认的实体 id。
  const [relFor, setRelFor] = useState<number | null>(null);
  // 对象浏览展开的实体 id（wire-entity-v2；cell 档专属——免费档由后端 403 如实）。
  const [browseFor, setBrowseFor] = useState<number | null>(null);
  // 域动作面板展开的实体 id（wire-domain-actions）。
  const [actionsFor, setActionsFor] = useState<number | null>(null);
  const [deleteFor, setDeleteFor] = useState<number | null>(null);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      const [entityList, datasetList, graphData] = await Promise.all([
        fetchEntities(),
        fetchDatasets(),
        fetchEntityGraph(),
      ]);
      setEntities(entityList);
      setDatasets(datasetList);
      setGraph(graphData);
    } catch (err) {
      setLoadError(apiErrorText(err, t("console.entities.errorTitle")));
    }
  }, [t]);

  useEffect(() => {
    void load();
  }, [load]);

  /** 图谱节点点击：滚动定位到对应实体卡（卡片 id 锚 `entity-card-{id}`）。 */
  const scrollToEntity = useCallback((entityId: number) => {
    document
      .getElementById(`entity-card-${entityId}`)
      ?.scrollIntoView?.({ behavior: "smooth", block: "center" });
  }, []);

  const edges = useMemo(() => graph?.edges ?? [], [graph]);

  return (
    <div data-testid="entities-page">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold">{t("console.entities.title")}</h1>
          <p className="mt-1 max-w-2xl text-xs text-slate-500">
            {t("console.entities.subtitle")}
          </p>
        </div>
        {!createOpen && (
          <button
            type="button"
            data-testid="entity-create-open"
            onClick={() => setCreateOpen(true)}
            className="rounded bg-slate-900 px-3 py-1 text-xs text-white hover:bg-slate-700"
          >
            {t("console.entities.createOpen")}
          </button>
        )}
      </div>

      {createOpen && (
        <CreateEntityForm
          datasets={datasets ?? []}
          onDone={() => {
            setCreateOpen(false);
            void load();
          }}
          onCancel={() => setCreateOpen(false)}
        />
      )}

      {loadError && (
        <div
          data-testid="entities-error"
          className="mt-4 rounded-md border border-red-200 bg-red-50 p-4"
        >
          <p className="text-sm text-red-700">{t("console.entities.errorTitle")}</p>
          {loadError !== t("console.entities.errorTitle") && (
            <p className="mt-1 break-all font-mono text-[11px] text-red-500">
              {loadError}
            </p>
          )}
          <button
            type="button"
            data-testid="entities-retry"
            onClick={() => void load()}
            className="mt-2 rounded border border-red-300 px-2 py-0.5 text-xs text-red-700 hover:bg-red-100"
          >
            {t("console.entities.retry")}
          </button>
        </div>
      )}

      {entities == null && !loadError && (
        <p className="mt-6 text-xs text-slate-400" data-testid="entities-loading">
          {t("console.entities.loading")}
        </p>
      )}

      {entities != null && entities.length === 0 && !loadError && (
        <div
          data-testid="entities-empty"
          className="mt-6 rounded-lg border border-dashed border-slate-300 p-8 text-center"
        >
          <p className="text-sm text-slate-600">{t("console.entities.empty")}</p>
          <p className="mt-1 text-xs text-slate-400">
            {t("console.entities.emptyHint")}
          </p>
        </div>
      )}

      {entities != null && entities.length > 0 && (
        <ul data-testid="entity-list" className="mt-4 space-y-2">
          {entities.map((entity) => {
            const rels = incidentRelationships(entity.id, edges);
            const tier = entity.dataset.storage_tier;
            return (
              <li
                key={entity.id}
                id={`entity-card-${entity.id}`}
                data-testid="entity-card"
                data-entity-id={entity.id}
                className="rounded-lg border border-slate-200 bg-white p-4"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span
                    data-testid="entity-name"
                    data-entity-id={entity.id}
                    className="text-sm font-medium text-slate-800"
                  >
                    {entity.name}
                  </span>
                  <span
                    data-testid="entity-dataset"
                    className="text-xs text-slate-500"
                  >
                    {entity.dataset.name}
                  </span>
                  <span
                    data-testid="entity-rows"
                    className="text-[11px] text-slate-400"
                  >
                    {t("console.datasets.rowCount", {
                      count: entity.dataset.row_count,
                    })}
                  </span>
                  {tier && (
                    <span
                      data-testid="entity-tier-badge"
                      data-tier={tier}
                      className="rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-600 ring-1 ring-slate-200"
                    >
                      {tier === "api" || tier === "cell"
                        ? t(`console.datasets.tierBadge.${tier}`)
                        : tier}
                    </span>
                  )}
                  <div className="ml-auto flex flex-wrap gap-2">
                    <button
                      type="button"
                      data-testid="entity-toggle-objects"
                      data-entity-id={entity.id}
                      onClick={() =>
                        setBrowseFor(browseFor === entity.id ? null : entity.id)
                      }
                      className="rounded border border-slate-300 px-2.5 py-1 text-xs text-slate-600 hover:bg-slate-50"
                    >
                      {t("console.entities.objects.toggle")}
                    </button>
                    <button
                      type="button"
                      data-testid="entity-toggle-actions"
                      data-entity-id={entity.id}
                      onClick={() =>
                        setActionsFor(actionsFor === entity.id ? null : entity.id)
                      }
                      className="rounded border border-slate-300 px-2.5 py-1 text-xs text-slate-600 hover:bg-slate-50"
                    >
                      {t("console.entities.actions.toggle")}
                    </button>
                    <button
                      type="button"
                      data-testid="entity-toggle-relationships"
                      data-entity-id={entity.id}
                      onClick={() =>
                        setRelFor(relFor === entity.id ? null : entity.id)
                      }
                      className="rounded border border-slate-300 px-2.5 py-1 text-xs text-slate-600 hover:bg-slate-50"
                    >
                      {t("console.entities.relationships.toggle", {
                        count: rels.length,
                      })}
                    </button>
                    <button
                      type="button"
                      data-testid="entity-delete"
                      data-entity-id={entity.id}
                      onClick={() => setDeleteFor(entity.id)}
                      className="rounded border border-rose-300 px-2.5 py-1 text-xs text-rose-700 hover:bg-rose-50"
                    >
                      {t("console.entities.card.delete")}
                    </button>
                  </div>
                </div>
                <div className="mt-2 flex flex-wrap gap-1">
                  {entity.pk_columns.map((column) => (
                    <span
                      key={column}
                      data-testid="entity-pk-badge"
                      data-column={column}
                      className="rounded bg-indigo-50 px-1.5 py-0.5 font-mono text-[11px] text-indigo-700 ring-1 ring-indigo-100"
                    >
                      {column}
                    </span>
                  ))}
                </div>
                {entity.description && (
                  <p
                    data-testid="entity-desc"
                    className="mt-2 text-xs leading-relaxed text-slate-500"
                  >
                    {entity.description}
                  </p>
                )}
                {browseFor === entity.id && (
                  <EntityObjectBrowser
                    key={`objects-${entity.id}`}
                    entity={entity}
                    edges={rels}
                  />
                )}
                {actionsFor === entity.id && (
                  <DomainActionsPanel
                    key={`actions-${entity.id}`}
                    entity={entity}
                    datasets={datasets ?? []}
                    onChanged={() => void load()}
                  />
                )}
                {relFor === entity.id && (
                  <RelationshipPanel
                    key={`rel-${entity.id}`}
                    entity={entity}
                    entities={entities}
                    datasets={datasets ?? []}
                    edges={edges}
                    onChanged={load}
                  />
                )}
                {deleteFor === entity.id && (
                  <DeleteEntityDialog
                    key={`del-${entity.id}`}
                    entity={entity}
                    relationshipCount={rels.length}
                    onDone={() => {
                      setDeleteFor(null);
                      void load();
                    }}
                    onCancel={() => setDeleteFor(null)}
                  />
                )}
              </li>
            );
          })}
        </ul>
      )}

      {/* SVG 小图谱：节点=实体、边=关系；空态如实文案（无实体/无关系均可空）。 */}
      <section className="mt-8" data-testid="entity-graph-section">
        <h2 className="text-sm font-semibold text-slate-700">
          {t("console.entities.graph.title")}
        </h2>
        <p className="mt-1 max-w-2xl text-xs text-slate-500">
          {t("console.entities.graph.subtitle")}
        </p>
        {graph == null ? (
          <p className="mt-3 text-xs text-slate-400" data-testid="entity-graph-loading">
            {t("console.entities.loading")}
          </p>
        ) : (
          <EntityMiniGraph graph={graph} onOpenNode={scrollToEntity} />
        )}
      </section>
    </div>
  );
}

/**
 * 创建实体表单（任务 3.2）：数据集下拉 → 主键列勾选 → 业务名 → 描述。
 * 前端先拦显性缺失（数据集/至少一列/业务名）；提交后 422 逐条 / 409 冲突
 * / 404 如实呈现（表单不关可改再试）；成功交 onDone（关闭 + 重载）。
 */
function CreateEntityForm({
  datasets,
  onDone,
  onCancel,
}: {
  datasets: Dataset[];
  onDone: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [datasetId, setDatasetId] = useState("");
  const [pkColumns, setPkColumns] = useState<ReadonlySet<string>>(new Set());
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [displayColumn, setDisplayColumn] = useState("");
  const [columnNotesText, setColumnNotesText] = useState("");
  const [creating, setCreating] = useState(false);
  const [errors, setErrors] = useState<string[] | null>(null);

  const dataset = datasets.find((d) => d.id === datasetId);

  /** 勾选/取消主键列（保持数据集列序，服务端重校验）。 */
  const toggleColumn = (column: string, checked: boolean) => {
    setPkColumns((prev) => {
      const next = new Set(prev);
      if (checked) next.add(column);
      else next.delete(column);
      return next;
    });
    setErrors(null);
  };

  const submit = async () => {
    if (creating) return;
    const lines: string[] = [];
    if (!datasetId) lines.push(t("console.entities.create.datasetRequired"));
    if (pkColumns.size === 0) lines.push(t("console.entities.create.pkRequired"));
    const trimmed = name.trim();
    if (!trimmed) lines.push(t("console.entities.create.nameRequired"));
    let notes: Record<string, string> | undefined;
    if (columnNotesText.trim()) {
      try {
        const parsed: unknown = JSON.parse(columnNotesText);
        if (parsed == null || typeof parsed !== "object" || Array.isArray(parsed)) {
          lines.push(t("console.entities.create.notesInvalid"));
        } else {
          notes = parsed as Record<string, string>;
        }
      } catch {
        lines.push(t("console.entities.create.notesInvalid"));
      }
    }
    if (lines.length > 0) {
      setErrors(lines);
      return;
    }
    setCreating(true);
    setErrors(null);
    try {
      await createEntity({
        name: trimmed,
        datasetId,
        // 按数据集列序提交（勾选集合本身无序）。
        pkColumns: (dataset?.columns ?? [])
          .map((c) => c.name)
          .filter((c) => pkColumns.has(c)),
        description: description.trim() || undefined,
        displayColumn: displayColumn || undefined,
        columnNotes: notes,
      });
      onDone();
    } catch (err) {
      setErrors(entityErrorLines(err, t("console.entities.create.failed")));
    } finally {
      setCreating(false);
    }
  };

  return (
    <form
      data-testid="entity-create-form"
      className="mt-4 rounded-lg border border-slate-200 bg-white p-4"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <h2 className="text-sm font-semibold text-slate-700">
        {t("console.entities.create.title")}
      </h2>

      <div className="mt-3">
        <label
          htmlFor="entity-create-dataset"
          className="block text-xs text-slate-500"
        >
          {t("console.entities.create.datasetLabel")}
        </label>
        <select
          id="entity-create-dataset"
          data-testid="entity-create-dataset"
          value={datasetId}
          disabled={creating}
          onChange={(e) => {
            setDatasetId(e.target.value);
            // 换数据集即清主键勾选（列清单随集而变）。
            setPkColumns(new Set());
            setErrors(null);
          }}
          className="mt-1 w-72 rounded-md border border-slate-300 px-2 py-1 text-sm disabled:opacity-50"
        >
          <option value="">
            {datasets.length === 0
              ? t("console.entities.create.noDatasets")
              : t("console.entities.create.datasetPlaceholder")}
          </option>
          {datasets.map((d) => (
            <option key={d.id} value={d.id}>
              {`${d.name} · ${t("console.datasets.rowCount", { count: d.row_count })}`}
            </option>
          ))}
        </select>
      </div>

      <div className="mt-3">
        <p className="text-xs text-slate-500">
          {t("console.entities.create.pkLabel")}
        </p>
        {dataset ? (
          <div
            data-testid="entity-create-pk-columns"
            className="mt-1.5 flex max-w-2xl flex-wrap gap-x-4 gap-y-1.5"
          >
            {dataset.columns.map((column) => (
              <label
                key={column.name}
                className="flex cursor-pointer items-center gap-1.5 text-xs text-slate-700"
              >
                <input
                  type="checkbox"
                  data-testid="entity-create-pk-check"
                  data-column={column.name}
                  checked={pkColumns.has(column.name)}
                  disabled={creating}
                  onChange={(e) => toggleColumn(column.name, e.target.checked)}
                  className="h-3.5 w-3.5 accent-slate-800"
                />
                <span className="font-mono">{column.name}</span>
              </label>
            ))}
          </div>
        ) : (
          <p
            data-testid="entity-create-pk-empty"
            className="mt-1.5 text-xs text-slate-400"
          >
            {t("console.entities.create.pkEmpty")}
          </p>
        )}
      </div>

      <div className="mt-3 flex flex-wrap gap-3">
        <div>
          <label
            htmlFor="entity-create-name"
            className="block text-xs text-slate-500"
          >
            {t("console.entities.create.nameLabel")}
          </label>
          <input
            id="entity-create-name"
            data-testid="entity-create-name"
            value={name}
            disabled={creating}
            onChange={(e) => {
              setName(e.target.value);
              setErrors(null);
            }}
            placeholder={t("console.entities.create.namePlaceholder")}
            className="mt-1 w-64 rounded-md border border-slate-300 px-2 py-1 text-sm disabled:opacity-50"
          />
        </div>
        <div>
          <label
            htmlFor="entity-create-desc"
            className="block text-xs text-slate-500"
          >
            {t("console.entities.create.descLabel")}
          </label>
          <input
            id="entity-create-desc"
            data-testid="entity-create-desc"
            value={description}
            disabled={creating}
            onChange={(e) => {
              setDescription(e.target.value);
              setErrors(null);
            }}
            placeholder={t("console.entities.create.descPlaceholder")}
            className="mt-1 w-72 rounded-md border border-slate-300 px-2 py-1 text-sm disabled:opacity-50"
          />
        </div>
      </div>

      {/* v2 属性元数据（wire-entity-v2）：显示列下拉 + 列注记（JSON 可选）。 */}
      <div className="mt-3 flex flex-wrap gap-3">
        <div>
          <label
            htmlFor="entity-create-display"
            className="block text-xs text-slate-500"
          >
            {t("console.entities.create.displayLabel")}
          </label>
          <select
            id="entity-create-display"
            data-testid="entity-create-display"
            value={displayColumn}
            disabled={creating || !dataset}
            onChange={(e) => {
              setDisplayColumn(e.target.value);
              setErrors(null);
            }}
            className="mt-1 w-56 rounded-md border border-slate-300 px-2 py-1 text-sm disabled:opacity-50"
          >
            <option value="">
              {t("console.entities.create.displayPlaceholder")}
            </option>
            {(dataset?.columns ?? []).map((column) => (
              <option key={column.name} value={column.name}>
                {column.name}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label
            htmlFor="entity-create-notes"
            className="block text-xs text-slate-500"
          >
            {t("console.entities.create.notesLabel")}
          </label>
          <input
            id="entity-create-notes"
            data-testid="entity-create-notes"
            value={columnNotesText}
            disabled={creating}
            onChange={(e) => {
              setColumnNotesText(e.target.value);
              setErrors(null);
            }}
            placeholder={t("console.entities.create.notesPlaceholder")}
            className="mt-1 w-72 rounded-md border border-slate-300 px-2 py-1 text-sm disabled:opacity-50"
          />
        </div>
      </div>

      <div className="mt-3 flex items-center gap-2">
        <button
          type="submit"
          data-testid="entity-create-submit"
          disabled={creating}
          className="rounded bg-slate-900 px-3 py-1 text-xs text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {creating
            ? t("console.entities.create.creating")
            : t("console.entities.create.submit")}
        </button>
        <button
          type="button"
          data-testid="entity-create-cancel"
          disabled={creating}
          onClick={onCancel}
          className="rounded border border-slate-300 px-3 py-1 text-xs text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {t("console.entities.create.cancel")}
        </button>
      </div>

      {errors && errors.length > 0 && (
        <ul
          data-testid="entity-create-errors"
          className="mt-2 space-y-0.5 rounded-md border border-red-200 bg-red-50/60 px-3 py-2"
        >
          {errors.map((line, i) => (
            <li key={i} className="text-[11px] text-red-600">
              {line}
            </li>
          ))}
        </ul>
      )}
    </form>
  );
}

/**
 * 删除实体确认（内嵌小对话框，照 DatasetDetail 删除确认范式）：确认文案带
 * 级联关系数（0 条时换零关系口径）；确认后 DELETE，响应的级联数量由重载
 * 后的图谱消失如实呈现。
 */
function DeleteEntityDialog({
  entity,
  relationshipCount,
  onDone,
  onCancel,
}: {
  entity: EntityItem;
  relationshipCount: number;
  onDone: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const confirmDelete = async () => {
    if (deleting) return;
    setDeleting(true);
    setError(null);
    try {
      await deleteEntity(entity.id);
      onDone();
    } catch (err) {
      setError(apiErrorText(err, t("console.entities.card.deleteFailed")));
      setDeleting(false);
    }
  };

  return (
    <div
      data-testid="entity-delete-dialog"
      role="dialog"
      className="mt-3 rounded-lg border border-rose-200 bg-rose-50/60 p-4"
    >
      <h3 className="text-sm font-semibold text-rose-800">
        {t("console.entities.card.deleteTitle")}
      </h3>
      <p data-testid="entity-delete-warning" className="mt-1.5 text-xs text-slate-600">
        {relationshipCount > 0
          ? t("console.entities.card.deleteWarning", {
              name: entity.name,
              count: relationshipCount,
            })
          : t("console.entities.card.deleteWarningZero", { name: entity.name })}
      </p>
      {error && (
        <p data-testid="entity-delete-error" className="mt-1.5 text-xs text-red-600">
          {error}
        </p>
      )}
      <div className="mt-3 flex gap-2">
        <button
          type="button"
          data-testid="entity-delete-confirm"
          disabled={deleting}
          onClick={() => void confirmDelete()}
          className="rounded bg-rose-600 px-3 py-1 text-xs text-white hover:bg-rose-700 disabled:cursor-not-allowed disabled:bg-rose-300"
        >
          {deleting
            ? t("console.entities.card.deleting")
            : t("console.entities.card.deleteConfirm")}
        </button>
        <button
          type="button"
          data-testid="entity-delete-cancel"
          disabled={deleting}
          onClick={onCancel}
          className="rounded border border-slate-300 bg-white px-3 py-1 text-xs text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {t("console.entities.card.cancel")}
        </button>
      </div>
    </div>
  );
}

/** kind → i18n key（"n:1"/"1:1" 含冒号不能直接作 i18n 键——ns 分隔符）。 */
const KIND_KEYS: Record<EntityRelationshipKind, string> = {
  "n:1": "console.entities.relationships.kinds.nTo1",
  "1:1": "console.entities.relationships.kinds.oneToOne",
  "1:n": "console.entities.relationships.kinds.oneToN",
  "m:n": "console.entities.relationships.kinds.mToN",
};

/**
 * 关系管理面板（每实体卡展开）：关系行 + 添加表单。表单纪律：目标实体下拉
 * 仅其他实体；源列下拉 = 本实体数据集列；目标列下拉 = 目标实体主键列（纪律）；
 * kind 二选一；label 可选。422/409 如实逐条呈现。
 */
function RelationshipPanel({
  entity,
  entities,
  datasets,
  edges,
  onChanged,
}: {
  entity: EntityItem;
  entities: EntityItem[];
  datasets: Dataset[];
  edges: ReadonlyArray<EntityRelationship>;
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  const rels = incidentRelationships(entity.id, edges);
  const nameById = useMemo(
    () => new Map(entities.map((e) => [e.id, e.name])),
    [entities],
  );
  // 图谱边两端实体反查（nodes=entities 同源）：物化按钮的档位双闸数据源。
  const entityById = useMemo(
    () => new Map(entities.map((e) => [e.id, e])),
    [entities],
  );
  const ownColumns = useMemo(() => {
    const dataset = datasets.find((d) => d.id === entity.dataset.id);
    return dataset ? dataset.columns.map((c) => c.name) : [];
  }, [datasets, entity.dataset.id]);

  // 添加表单状态。
  const [toEntityId, setToEntityId] = useState<number | null>(null);
  const [fromColumn, setFromColumn] = useState("");
  const [toColumn, setToColumn] = useState("");
  const [kind, setKind] = useState<EntityRelationshipKind>("n:1");
  const [label, setLabel] = useState("");
  const [junctionDatasetId, setJunctionDatasetId] = useState("");
  const [junctionSourceColumn, setJunctionSourceColumn] = useState("");
  const [junctionTargetColumn, setJunctionTargetColumn] = useState("");
  const [adding, setAdding] = useState(false);
  const [errors, setErrors] = useState<string[] | null>(null);
  const [removeError, setRemoveError] = useState<string | null>(null);
  // 物化连接状态（wire-entity-link 3.1）：单飞行中关系、成功信息、失败逐条。
  const [materializingRelId, setMaterializingRelId] = useState<number | null>(
    null,
  );
  const [materializeDone, setMaterializeDone] = useState<{
    name: string;
    /** m:n 桥展开诚实计数（wire-entity-mn-materialize；n:1/1:1 无此字段）。 */
    bridge?: { estimate_rows: number; rows: number; orphan_dropped: number } | null;
  } | null>(
    null,
  );
  const [materializeErrors, setMaterializeErrors] = useState<string[] | null>(
    null,
  );

  /** 物化动作可见性：两端实体数据集摘要的 storage_tier 均 "cell"
   *  （免费档 api 数据集不出现该动作——服务端另有 403 档位闸兜底）。 */
  const canMaterialize = (rel: EntityRelationship): boolean =>
    entityById.get(rel.from_entity_id)?.dataset.storage_tier === "cell" &&
    entityById.get(rel.to_entity_id)?.dataset.storage_tier === "cell";

  const materialize = async (relId: number) => {
    if (materializingRelId != null) return;
    setMaterializingRelId(relId);
    setMaterializeErrors(null);
    setMaterializeDone(null);
    try {
      const dataset = await materializeEntityRelationship(entity.id, relId);
      setMaterializeDone({
        name: dataset.name,
        bridge: dataset.materialization ?? null,
      });
      // 成功后重载：新连接数据集入「我的数据」（实体/图谱态刷新）。
      onChanged();
    } catch (err) {
      setMaterializeErrors(
        entityErrorLines(err, t("console.entities.materialize.failed")),
      );
    } finally {
      setMaterializingRelId(null);
    }
  };

  // 目标实体下拉 = 其他实体（spec 场景：仅本企业其他实体）。
  const targetOptions = entities.filter((e) => e.id !== entity.id);
  // 纪律：目标列下拉 = 目标实体的主键列（不是目标数据集全部列）。
  const targetPkColumns =
    targetOptions.find((e) => e.id === toEntityId)?.pk_columns ?? [];

  const submit = async () => {
    if (adding) return;
    const lines: string[] = [];
    if (toEntityId == null) lines.push(t("console.entities.relationships.targetRequired"));
    if (!fromColumn) lines.push(t("console.entities.relationships.fromRequired"));
    if (!toColumn) lines.push(t("console.entities.relationships.toRequired"));
    if (lines.length > 0) {
      setErrors(lines);
      return;
    }
    setAdding(true);
    setErrors(null);
    try {
      await createEntityRelationship(entity.id, {
        toEntityId: toEntityId!,
        fromColumn,
        toColumn,
        kind,
        label: label.trim() || undefined,
        junctionDatasetId: junctionDatasetId || undefined,
        junctionSourceColumn: junctionSourceColumn || undefined,
        junctionTargetColumn: junctionTargetColumn || undefined,
      });
      onChanged();
    } catch (err) {
      setErrors(entityErrorLines(err, t("console.entities.relationships.failed")));
    } finally {
      setAdding(false);
    }
  };

  const remove = async (relId: number) => {
    if (adding) return;
    setRemoveError(null);
    try {
      // rel 以本实体为端点（面板只列端点关系），路径实体即本实体。
      await deleteEntityRelationship(entity.id, relId);
      onChanged();
    } catch (err) {
      setRemoveError(apiErrorText(err, t("console.entities.relationships.removeFailed")));
    }
  };

  return (
    <div
      data-testid="entity-relationships"
      data-entity-id={entity.id}
      className="mt-3 rounded-md border border-slate-200 bg-slate-50/60 p-3"
    >
      <h3 className="text-xs font-semibold text-slate-600">
        {t("console.entities.relationships.title")}
      </h3>

      {rels.length === 0 ? (
        <p data-testid="entity-relationships-empty" className="mt-1.5 text-xs text-slate-400">
          {t("console.entities.relationships.empty")}
        </p>
      ) : (
        <ul data-testid="entity-relationship-list" className="mt-2 space-y-1">
          {rels.map((rel) => (
            <li
              key={rel.id}
              data-testid="entity-relationship-row"
              data-rel-id={rel.id}
              className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-slate-700"
            >
              <span className="font-mono">
                {nameById.get(rel.from_entity_id) ?? `#${rel.from_entity_id}`}.
                {rel.from_column}
              </span>
              <span
                data-testid="entity-relationship-kind"
                data-kind={rel.kind}
                className="rounded bg-slate-200/70 px-1.5 py-0.5 font-mono text-[11px] text-slate-600"
              >
                →({rel.kind})
              </span>
              <span className="font-mono">
                {nameById.get(rel.to_entity_id) ?? `#${rel.to_entity_id}`}.
                {rel.to_column}
              </span>
              {rel.label && (
                <span
                  data-testid="entity-relationship-label"
                  className="text-[11px] text-slate-400"
                >
                  {rel.label}
                </span>
              )}
              <div className="ml-auto flex flex-wrap gap-1.5">
                {/* 物化连接（wire-entity-link）：双 cell 档才渲染——LEFT JOIN
                    语义一键生成连接数据集，成功内联提示 + 跳「我的数据」。 */}
                {canMaterialize(rel) && (
                  <button
                    type="button"
                    data-testid="entity-relationship-materialize"
                    data-rel-id={rel.id}
                    disabled={materializingRelId != null}
                    title={t("console.entities.materialize.hint")}
                    onClick={() => void materialize(rel.id)}
                    className="rounded border border-indigo-300 bg-white px-1.5 py-0.5 text-[11px] text-indigo-700 hover:bg-indigo-50 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {materializingRelId === rel.id
                      ? t("console.entities.materialize.working")
                      : t("console.entities.materialize.action")}
                  </button>
                )}
                <button
                  type="button"
                  data-testid="entity-relationship-delete"
                  data-rel-id={rel.id}
                  onClick={() => void remove(rel.id)}
                  className="rounded border border-rose-300 bg-white px-1.5 py-0.5 text-[11px] text-rose-700 hover:bg-rose-50"
                >
                  {t("console.entities.card.delete")}
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      {/* 物化成功：新数据集名 + 「查看」跳我的数据（数据集列表已含连接子集）。 */}
      {materializeDone && (
        <div
          data-testid="entity-relationship-materialize-done"
          className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 rounded-md border border-emerald-200 bg-emerald-50/60 px-3 py-2"
        >
          <span className="text-[11px] text-emerald-800">
            {materializeDone.bridge
              ? t("console.entities.materialize.doneBridge", {
                  name: materializeDone.name,
                  rows: materializeDone.bridge.rows,
                  orphan: materializeDone.bridge.orphan_dropped,
                })
              : t("console.entities.materialize.done", {
                  name: materializeDone.name,
                })}
          </span>
          <a
            href="#/console/datasets"
            data-testid="entity-relationship-materialize-view"
            className="rounded border border-emerald-300 bg-white px-1.5 py-0.5 text-[11px] text-emerald-700 hover:bg-emerald-100"
          >
            {t("console.entities.materialize.view")}
          </a>
        </div>
      )}

      {/* 物化失败：detail 逐条 / 字符串如实（403 档位闸 / 422 列漂移 / 502 旧 cell）。 */}
      {materializeErrors && materializeErrors.length > 0 && (
        <ul
          data-testid="entity-relationship-materialize-errors"
          className="mt-2 space-y-0.5"
        >
          {materializeErrors.map((line, i) => (
            <li key={i} className="text-[11px] text-red-600">
              {line}
            </li>
          ))}
        </ul>
      )}

      {removeError && (
        <p data-testid="entity-relationship-remove-error" className="mt-1.5 text-[11px] text-red-600">
          {removeError}
        </p>
      )}

      {/* 添加关系表单（纪律注记如实呈现）。 */}
      <div
        data-testid="entity-relationship-form"
        className="mt-3 rounded border border-slate-200 bg-white p-3"
      >
        <h4 className="text-xs font-semibold text-slate-600">
          {t("console.entities.relationships.addTitle")}
        </h4>
        <p className="mt-1 text-[11px] text-slate-400">
          {t("console.entities.relationships.disciplineNote")}
        </p>
        <div className="mt-2 flex flex-wrap items-end gap-3">
          <div>
            <label
              htmlFor={`rel-target-${entity.id}`}
              className="block text-[11px] text-slate-500"
            >
              {t("console.entities.relationships.targetLabel")}
            </label>
            <select
              id={`rel-target-${entity.id}`}
              data-testid="entity-relationship-target"
              value={toEntityId ?? ""}
              disabled={adding}
              onChange={(e) => {
                // 换目标实体即清目标列（目标主键集合随目标而变）。
                setToEntityId(e.target.value ? Number(e.target.value) : null);
                setToColumn("");
                setErrors(null);
              }}
              className="mt-1 w-44 rounded-md border border-slate-300 px-2 py-1 text-sm disabled:opacity-50"
            >
              <option value="">
                {t("console.entities.relationships.targetPlaceholder")}
              </option>
              {targetOptions.map((target) => (
                <option key={target.id} value={target.id}>
                  {target.name}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label
              htmlFor={`rel-from-${entity.id}`}
              className="block text-[11px] text-slate-500"
            >
              {t("console.entities.relationships.fromColumnLabel")}
            </label>
            <select
              id={`rel-from-${entity.id}`}
              data-testid="entity-relationship-from"
              value={fromColumn}
              disabled={adding}
              onChange={(e) => {
                setFromColumn(e.target.value);
                setErrors(null);
              }}
              className="mt-1 w-44 rounded-md border border-slate-300 px-2 py-1 font-mono text-sm disabled:opacity-50"
            >
              <option value="">
                {t("console.entities.relationships.fromColumnPlaceholder")}
              </option>
              {ownColumns.map((column) => (
                <option key={column} value={column}>
                  {column}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label
              htmlFor={`rel-to-${entity.id}`}
              className="block text-[11px] text-slate-500"
            >
              {t("console.entities.relationships.toColumnLabel")}
            </label>
            <select
              id={`rel-to-${entity.id}`}
              data-testid="entity-relationship-to"
              value={toColumn}
              disabled={adding || toEntityId == null}
              onChange={(e) => {
                setToColumn(e.target.value);
                setErrors(null);
              }}
              className="mt-1 w-40 rounded-md border border-slate-300 px-2 py-1 font-mono text-sm disabled:opacity-50"
            >
              <option value="">
                {t("console.entities.relationships.toColumnPlaceholder")}
              </option>
              {targetPkColumns.map((column) => (
                <option key={column} value={column}>
                  {column}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label
              htmlFor={`rel-kind-${entity.id}`}
              className="block text-[11px] text-slate-500"
            >
              {t("console.entities.relationships.kindLabel")}
            </label>
            <select
              id={`rel-kind-${entity.id}`}
              data-testid="entity-relationship-kind-select"
              value={kind}
              disabled={adding}
              onChange={(e) =>
                setKind(e.target.value as EntityRelationshipKind)
              }
              className="mt-1 w-36 rounded-md border border-slate-300 px-2 py-1 text-sm disabled:opacity-50"
            >
              {ENTITY_RELATIONSHIP_KINDS.map((value) => (
                <option key={value} value={value}>
                  {t(KIND_KEYS[value])}
                </option>
              ))}
            </select>
          </div>
          {kind === "m:n" && (
            <div className="flex flex-wrap items-end gap-2">
              <div>
                <label
                  htmlFor={`rel-junction-${entity.id}`}
                  className="block text-[11px] text-slate-500"
                >
                  {t("console.entities.relationships.junctionLabel")}
                </label>
                <select
                  id={`rel-junction-${entity.id}`}
                  data-testid="entity-relationship-junction"
                  value={junctionDatasetId}
                  disabled={adding}
                  onChange={(e) => setJunctionDatasetId(e.target.value)}
                  className="mt-1 w-48 rounded-md border border-slate-300 px-2 py-1 text-sm disabled:opacity-50"
                >
                  <option value="">
                    {t("console.entities.relationships.junctionPlaceholder")}
                  </option>
                  {datasets.map((d) => (
                    <option key={d.id} value={d.id}>
                      {d.name}
                    </option>
                  ))}
                </select>
              </div>
              <input
                data-testid="entity-relationship-junction-source"
                value={junctionSourceColumn}
                disabled={adding}
                onChange={(e) => setJunctionSourceColumn(e.target.value)}
                placeholder={t(
                  "console.entities.relationships.junctionSource",
                )}
                className="w-36 rounded-md border border-slate-300 px-2 py-1 text-sm disabled:opacity-50"
              />
              <input
                data-testid="entity-relationship-junction-target"
                value={junctionTargetColumn}
                disabled={adding}
                onChange={(e) => setJunctionTargetColumn(e.target.value)}
                placeholder={t(
                  "console.entities.relationships.junctionTarget",
                )}
                className="w-36 rounded-md border border-slate-300 px-2 py-1 text-sm disabled:opacity-50"
              />
            </div>
          )}
          <div>
            <label
              htmlFor={`rel-label-${entity.id}`}
              className="block text-[11px] text-slate-500"
            >
              {t("console.entities.relationships.labelLabel")}
            </label>
            <input
              id={`rel-label-${entity.id}`}
              data-testid="entity-relationship-label-input"
              value={label}
              disabled={adding}
              onChange={(e) => {
                setLabel(e.target.value);
                setErrors(null);
              }}
              placeholder={t("console.entities.relationships.labelPlaceholder")}
              className="mt-1 w-40 rounded-md border border-slate-300 px-2 py-1 text-sm disabled:opacity-50"
            />
          </div>
          <button
            type="button"
            data-testid="entity-relationship-submit"
            disabled={adding}
            onClick={() => void submit()}
            className="rounded bg-slate-900 px-3 py-1 text-xs text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {adding
              ? t("console.entities.relationships.adding")
              : t("console.entities.relationships.submit")}
          </button>
        </div>
        {errors && errors.length > 0 && (
          <ul
            data-testid="entity-relationship-errors"
            className="mt-2 space-y-0.5"
          >
            {errors.map((line, i) => (
              <li key={i} className="text-[11px] text-red-600">
                {line}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

// ── SVG 小图谱（v0 轻实现：环形布局 + 线边 + kind 小标注）──────────────

const GRAPH_WIDTH = 640;
const GRAPH_HEIGHT = 360;
const NODE_WIDTH = 128;
const NODE_HEIGHT = 44;

/** 环形布局坐标（单节点居中，多节点等角分布；节点点击滚动定位见上层）。 */
export function entityGraphLayout(
  count: number,
): Array<{ x: number; y: number }> {
  if (count <= 0) return [];
  if (count === 1) return [{ x: GRAPH_WIDTH / 2, y: GRAPH_HEIGHT / 2 }];
  const radius = Math.min(GRAPH_WIDTH, GRAPH_HEIGHT) / 2 - 76;
  const cx = GRAPH_WIDTH / 2;
  const cy = GRAPH_HEIGHT / 2;
  return Array.from({ length: count }, (_, i) => {
    const angle = -Math.PI / 2 + (i * 2 * Math.PI) / count;
    return { x: cx + radius * Math.cos(angle), y: cy + radius * Math.sin(angle) };
  });
}

/** 节点内业务名截断（SVG 内不换行——超长截断如实保留前缀）。 */
function shorten(name: string): string {
  return name.length > 14 ? `${name.slice(0, 13)}…` : name;
}

/**
 * SVG 小图谱：节点 = 实体（圆角矩形：业务名 + 数据集名小字），边 = 关系
 * （线 + kind 小标注；自环画小回环圈）。先画边后画节点（节点白底压线）。
 */
function EntityMiniGraph({
  graph,
  onOpenNode,
}: {
  graph: EntityGraph;
  onOpenNode: (entityId: number) => void;
}) {
  const { t } = useTranslation();
  const positions = useMemo(
    () => entityGraphLayout(graph.nodes.length),
    [graph.nodes.length],
  );
  const posById = useMemo(() => {
    const map = new Map<number, { x: number; y: number }>();
    graph.nodes.forEach((node, i) => map.set(node.id, positions[i]!));
    return map;
  }, [graph.nodes, positions]);

  if (graph.nodes.length === 0) {
    return (
      <p
        data-testid="entity-graph-empty"
        className="mt-3 rounded-md border border-dashed border-slate-300 bg-slate-50/60 px-3 py-2 text-xs text-slate-600"
      >
        {t("console.entities.graph.empty")}
      </p>
    );
  }

  return (
    <div
      data-testid="entity-graph"
      className="mt-3 overflow-x-auto rounded-lg border border-slate-200 bg-slate-50/40 p-2"
    >
      <svg
        viewBox={`0 0 ${GRAPH_WIDTH} ${GRAPH_HEIGHT}`}
        width={GRAPH_WIDTH}
        height={GRAPH_HEIGHT}
        role="img"
        aria-label={t("console.entities.graph.title")}
      >
        {graph.edges.map((edge) => {
          const a = posById.get(edge.from_entity_id);
          const b = posById.get(edge.to_entity_id);
          if (!a || !b) return null;
          const annotation = edge.label ? `${edge.kind} · ${edge.label}` : edge.kind;
          if (edge.from_entity_id === edge.to_entity_id) {
            // 自环（自引用如 员工.manager_id → 员工.id）：节点上方小回环圈。
            return (
              <g
                key={edge.id}
                data-testid="entity-graph-edge"
                data-from={edge.from_entity_id}
                data-to={edge.to_entity_id}
                data-kind={edge.kind}
              >
                <circle
                  cx={a.x}
                  cy={a.y - NODE_HEIGHT / 2 - 14}
                  r={14}
                  fill="none"
                  strokeWidth="1.5"
                  className="stroke-slate-300"
                />
                <text
                  x={a.x}
                  y={a.y - NODE_HEIGHT / 2 - 34}
                  textAnchor="middle"
                  fontSize="10"
                  className="fill-slate-500"
                >
                  {annotation}
                </text>
              </g>
            );
          }
          const mx = (a.x + b.x) / 2;
          const my = (a.y + b.y) / 2;
          return (
            <g
              key={edge.id}
              data-testid="entity-graph-edge"
              data-from={edge.from_entity_id}
              data-to={edge.to_entity_id}
              data-kind={edge.kind}
            >
              <title>{annotation}</title>
              <line
                x1={a.x}
                y1={a.y}
                x2={b.x}
                y2={b.y}
                strokeWidth="1.5"
                className="stroke-slate-300"
              />
              <text
                x={mx}
                y={my - 5}
                textAnchor="middle"
                fontSize="10"
                className="fill-slate-500"
              >
                {annotation}
              </text>
            </g>
          );
        })}
        {graph.nodes.map((node, i) => {
          const p = positions[i]!;
          return (
            <g
              key={node.id}
              data-testid="entity-graph-node"
              data-entity-id={node.id}
              onClick={() => onOpenNode(node.id)}
              className="cursor-pointer"
            >
              <title>{`${node.name} · ${node.dataset.name}`}</title>
              <rect
                x={p.x - NODE_WIDTH / 2}
                y={p.y - NODE_HEIGHT / 2}
                width={NODE_WIDTH}
                height={NODE_HEIGHT}
                rx={8}
                strokeWidth="1.5"
                className="fill-white stroke-slate-400"
              />
              <text
                x={p.x}
                y={p.y - 2}
                textAnchor="middle"
                fontSize="12"
                className="fill-slate-800"
              >
                {shorten(node.name)}
              </text>
              <text
                x={p.x}
                y={p.y + 13}
                textAnchor="middle"
                fontSize="9"
                className="fill-slate-400"
              >
                {shorten(node.dataset.name)}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
