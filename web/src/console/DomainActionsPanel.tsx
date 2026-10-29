// 实体域动作面板（wire-domain-actions）：类型声明构建器 + 实例台账 +
// 在途确认（复用闸既有 approve/reject 端点——单一执行路径，无第二确认面）。
// viewer 只读由后端角色门如实返回；镜像禁令：本面板不向壹座推送任何确认。
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  apiErrorText,
  bearerHeaders,
  ConsoleApiError,
  EntityItem,
} from "../console/api";

/** fetch 薄封装（api.ts requestJson 未导出；Bearer 头同款纪律，错误统一
 *  转 ConsoleApiError——detail 取响应体 detail）。 */
async function jsonRequest<T>(url: string, method: string, body?: unknown): Promise<T> {
  const response = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json", ...bearerHeaders() },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const payload: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail =
      typeof payload === "object" && payload !== null && "detail" in payload
        ? String((payload as { detail: unknown }).detail)
        : `HTTP ${response.status}`;
    throw new ConsoleApiError(detail, response.status, detail);
  }
  return payload as T;
}

/** 动作类型（api.ts 类型放这里会与 EntityItem 循环——本地最小声明）。 */
export interface DomainActionTypeItem {
  id: number;
  entity_id: number;
  name: string;
  risk: string;
  form_schema: { fields: DomainActionField[] };
  effect: { kind: string; value_mapping: Record<string, Record<string, string>> };
  enabled: boolean;
  dataset_id: string;
  pk_columns_json: string[];
}

export interface DomainActionField {
  name: string;
  label?: string;
  input: "text" | "number" | "date" | "select";
  required: boolean;
  options?: string[];
}

export interface DomainActionInstanceItem {
  id: number;
  type_id: number;
  proposal_id: number | null;
  proposer_sub: string;
  channel: string;
  source_ref: string | null;
  status: string;
  confirmed_by: string | null;
  correction_id: number | null;
  error: string | null;
}

const STATUS_STYLES: Record<string, string> = {
  awaiting_confirmation: "bg-amber-50 text-amber-700",
  executed: "bg-emerald-50 text-emerald-700",
  rejected: "bg-slate-100 text-slate-500",
  failed: "bg-rose-50 text-rose-700",
  disabled: "bg-slate-100 text-slate-500",
};

interface Props {
  entity: EntityItem;
  datasets: { id: string; name: string; columns?: { name: string }[] }[];
  onChanged: () => void;
}

export function DomainActionsPanel({ entity, datasets, onChanged }: Props) {
  const { t } = useTranslation();
  const [types, setTypes] = useState<DomainActionTypeItem[] | null>(null);
  const [instances, setInstances] = useState<DomainActionInstanceItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // 声明构建器状态
  const [name, setName] = useState("");
  const [risk, setRisk] = useState<"write" | "destructive">("write");
  const [fields, setFields] = useState<{ name: string; input: string; required: boolean; options: string }[]>([
    { name: "", input: "text", required: true, options: "" },
  ]);
  const [effectKind, setEffectKind] = useState<"insert" | "update">("insert");
  const [mappings, setMappings] = useState<{ column: string; bound: "field" | "literal"; field: string; literal: string }[]>([
    { column: "", bound: "field", field: "", literal: "" },
  ]);

  const boundDataset = datasets.find((d) => d.id === entity.dataset.id);
  const datasetColumns: string[] = (boundDataset?.columns ?? []).map((c) => c.name);

  const reload = useCallback(async () => {
    try {
      const data = await jsonRequest<{ actions: DomainActionTypeItem[] }>(
        `/api/v1/entities/${entity.id}/actions`,
        "GET",
      );
      setTypes(data.actions);
      const ledger = await jsonRequest<{ instances: DomainActionInstanceItem[] }>(
        "/api/v1/domain-actions/instances",
        "GET",
      );
      const typeIds = new Set(data.actions.map((a) => a.id));
      setInstances(ledger.instances.filter((i) => typeIds.has(i.type_id)));
    } catch (err) {
      setError(apiErrorText(err, t("console.entities.actions.loadFailed")));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 面板生命周期内一次装载
  }, [entity.id, t]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const submitType = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const cleanFields = fields
        .filter((f) => f.name.trim())
        .map((f) => ({
          name: f.name.trim(),
          input: f.input,
          required: f.required,
          ...(f.input === "select"
            ? { options: f.options.split(",").map((o) => o.trim()).filter(Boolean) }
            : {}),
        }));
      const valueMapping: Record<string, Record<string, string>> = {};
      for (const m of mappings) {
        if (!m.column.trim()) continue;
        valueMapping[m.column.trim()] =
          m.bound === "field"
            ? { field: m.field.trim() }
            : { literal: m.literal };
      }
      await jsonRequest(`/api/v1/entities/${entity.id}/actions`, "POST", {
        name: name.trim(),
        risk,
        form_schema: { fields: cleanFields },
        effect: { kind: effectKind, value_mapping: valueMapping },
      });
      setName("");
      setRisk("write");
      setFields([{ name: "", input: "text", required: true, options: "" }]);
      setMappings([{ column: "", bound: "field", field: "", literal: "" }]);
      await reload();
      onChanged();
    } catch (err) {
      if (err instanceof ConsoleApiError) {
        setError(apiErrorText(err, t("console.entities.actions.createFailed")));
      } else {
        setError(apiErrorText(err, t("console.entities.actions.createFailed")));
      }
    } finally {
      setBusy(false);
    }
  };

  const disableType = async (typeId: number) => {
    if (busy) return;
    setBusy(true);
    try {
      await jsonRequest(`/api/v1/entities/${entity.id}/actions/${typeId}`, "DELETE");
      await reload();
      onChanged();
    } catch (err) {
      setError(apiErrorText(err, t("console.entities.actions.disableFailed")));
    } finally {
      setBusy(false);
    }
  };

  const confirmInstance = async (instance: DomainActionInstanceItem, approve: boolean) => {
    if (busy || instance.proposal_id == null) return;
    setBusy(true);
    setError(null);
    try {
      await jsonRequest(
        `/api/v1/ai/actions/${instance.proposal_id}/${approve ? "approve" : "reject"}`,
        "POST",
        {},
      );
      await reload();
      onChanged();
    } catch (err) {
      setError(apiErrorText(err, t("console.entities.actions.confirmFailed")));
    } finally {
      setBusy(false);
    }
  };

  const typeName = (typeId: number): string =>
    types?.find((a) => a.id === typeId)?.name ?? `#${typeId}`;

  return (
    <div
      data-testid="entity-actions-panel"
      data-entity-id={entity.id}
      className="mt-3 rounded-md border border-slate-200 bg-slate-50/60 p-3"
    >
      {/* 类型清单 */}
      <div data-testid="entity-actions-types">
        {(types ?? []).map((typeRow) => (
          <div
            key={typeRow.id}
            className="flex flex-wrap items-center gap-2 border-b border-slate-100 py-1.5 last:border-b-0"
          >
            <span className="text-xs font-medium text-slate-700">{typeRow.name}</span>
            <span
              data-testid={`domain-action-risk-${typeRow.id}`}
              className="rounded bg-indigo-50 px-1.5 py-0.5 text-[11px] text-indigo-700"
            >
              {typeRow.risk}
            </span>
            <span className="font-mono text-[11px] text-slate-400">
              {typeRow.effect.kind} → {typeRow.dataset_id.slice(0, 8)}
            </span>
            {!typeRow.enabled && (
              <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[11px] text-slate-500">
                {t("console.entities.actions.disabled")}
              </span>
            )}
            {typeRow.enabled && (
              <button
                type="button"
                data-testid={`domain-action-disable-${typeRow.id}`}
                onClick={() => void disableType(typeRow.id)}
                className="ml-auto rounded border border-slate-300 px-2 py-0.5 text-[11px] text-slate-600 hover:bg-white"
              >
                {t("console.entities.actions.disable")}
              </button>
            )}
          </div>
        ))}
        {types !== null && types.length === 0 && (
          <p className="text-xs text-slate-400">
            {t("console.entities.actions.noTypes")}
          </p>
        )}
      </div>

      {error && (
        <p data-testid="entity-actions-error" className="mt-2 text-xs text-rose-700">
          {error}
        </p>
      )}

      {/* 声明构建器 */}
      <div className="mt-3 rounded border border-slate-200 bg-white p-2.5">
        <p className="text-xs font-semibold text-slate-600">
          {t("console.entities.actions.declareTitle")}
        </p>
        <div className="mt-2 flex flex-wrap items-end gap-2">
          <input
            data-testid="domain-action-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={t("console.entities.actions.namePlaceholder")}
            className="w-40 rounded border border-slate-300 px-2 py-1 text-sm"
          />
          <select
            data-testid="domain-action-risk"
            value={risk}
            onChange={(e) => setRisk(e.target.value as "write" | "destructive")}
            className="rounded border border-slate-300 px-2 py-1 text-sm"
          >
            <option value="write">{t("console.entities.actions.riskWrite")}</option>
            <option value="destructive">
              {t("console.entities.actions.riskDestructive")}
            </option>
          </select>
        </div>
        <p className="mt-2 text-[11px] text-slate-500">
          {t("console.entities.actions.fieldsLabel")}
        </p>
        {fields.map((field, index) => (
          <div key={index} className="mt-1 flex flex-wrap items-center gap-1.5">
            <input
              data-testid="domain-action-field-name"
              value={field.name}
              onChange={(e) => {
                const next = [...fields];
                next[index] = { ...field, name: e.target.value };
                setFields(next);
              }}
              placeholder={t("console.entities.actions.fieldName")}
              className="w-32 rounded border border-slate-300 px-2 py-1 text-xs"
            />
            <select
              value={field.input}
              onChange={(e) => {
                const next = [...fields];
                next[index] = { ...field, input: e.target.value };
                setFields(next);
              }}
              className="rounded border border-slate-300 px-1.5 py-1 text-xs"
            >
              <option value="text">text</option>
              <option value="number">number</option>
              <option value="date">date</option>
              <option value="select">select</option>
            </select>
            <label className="flex items-center gap-1 text-[11px] text-slate-500">
              <input
                type="checkbox"
                checked={field.required}
                onChange={(e) => {
                  const next = [...fields];
                  next[index] = { ...field, required: e.target.checked };
                  setFields(next);
                }}
              />
              {t("console.entities.actions.required")}
            </label>
            {field.input === "select" && (
              <input
                value={field.options}
                onChange={(e) => {
                  const next = [...fields];
                  next[index] = { ...field, options: e.target.value };
                  setFields(next);
                }}
                placeholder={t("console.entities.actions.optionsCsv")}
                className="w-40 rounded border border-slate-300 px-2 py-1 text-xs"
              />
            )}
          </div>
        ))}
        <button
          type="button"
          onClick={() =>
            setFields([...fields, { name: "", input: "text", required: false, options: "" }])
          }
          className="mt-1 rounded border border-slate-300 px-2 py-0.5 text-[11px] text-slate-600"
        >
          {t("console.entities.actions.addField")}
        </button>
        <p className="mt-2 text-[11px] text-slate-500">
          {t("console.entities.actions.effectLabel")}
        </p>
        <select
          data-testid="domain-action-effect-kind"
          value={effectKind}
          onChange={(e) => setEffectKind(e.target.value as "insert" | "update")}
          className="rounded border border-slate-300 px-1.5 py-1 text-xs"
        >
          <option value="insert">insert</option>
          <option value="update">update</option>
        </select>
        {mappings.map((m, index) => (
          <div key={index} className="mt-1 flex flex-wrap items-center gap-1.5">
            <select
              data-testid="domain-action-mapping-column"
              value={m.column}
              onChange={(e) => {
                const next = [...mappings];
                next[index] = { ...m, column: e.target.value };
                setMappings(next);
              }}
              className="w-36 rounded border border-slate-300 px-1.5 py-1 text-xs"
            >
              <option value="">{t("console.entities.actions.columnPlaceholder")}</option>
              {datasetColumns.map((column) => (
                <option key={column} value={column}>
                  {column}
                </option>
              ))}
            </select>
            <select
              value={m.bound}
              onChange={(e) => {
                const next = [...mappings];
                next[index] = { ...m, bound: e.target.value as "field" | "literal" };
                setMappings(next);
              }}
              className="rounded border border-slate-300 px-1.5 py-1 text-xs"
            >
              <option value="field">field</option>
              <option value="literal">literal</option>
            </select>
            {m.bound === "field" ? (
              <select
                value={m.field}
                onChange={(e) => {
                  const next = [...mappings];
                  next[index] = { ...m, field: e.target.value };
                  setMappings(next);
                }}
                className="w-32 rounded border border-slate-300 px-1.5 py-1 text-xs"
              >
                <option value="">{t("console.entities.actions.fieldPlaceholder")}</option>
                {fields
                  .filter((f) => f.name.trim())
                  .map((f) => (
                    <option key={f.name} value={f.name}>
                      {f.name}
                    </option>
                  ))}
              </select>
            ) : (
              <input
                value={m.literal}
                onChange={(e) => {
                  const next = [...mappings];
                  next[index] = { ...m, literal: e.target.value };
                  setMappings(next);
                }}
                placeholder={t("console.entities.actions.literalPlaceholder")}
                className="w-32 rounded border border-slate-300 px-2 py-1 text-xs"
              />
            )}
          </div>
        ))}
        <button
          type="button"
          onClick={() =>
            setMappings([
              ...mappings,
              { column: "", bound: "field", field: "", literal: "" },
            ])
          }
          className="mt-1 rounded border border-slate-300 px-2 py-0.5 text-[11px] text-slate-600"
        >
          {t("console.entities.actions.addMapping")}
        </button>
        <div className="mt-2">
          <button
            type="button"
            data-testid="domain-action-create"
            disabled={busy}
            onClick={() => void submitType()}
            className="rounded bg-slate-800 px-3 py-1 text-xs text-white disabled:opacity-50"
          >
            {t("console.entities.actions.declareSubmit")}
          </button>
        </div>
      </div>

      {/* 实例台账 */}
      <div className="mt-3" data-testid="entity-actions-instances">
        <p className="text-xs font-semibold text-slate-600">
          {t("console.entities.actions.instancesTitle")}
        </p>
        {instances.map((instance) => (
          <div
            key={instance.id}
            data-testid="domain-action-instance"
            className="mt-1.5 rounded border border-slate-100 bg-white px-2 py-1.5"
          >
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <span className="font-medium text-slate-700">
                {typeName(instance.type_id)}
              </span>
              <span
                data-status={instance.status}
                className={`rounded px-1.5 py-0.5 text-[11px] ${
                  STATUS_STYLES[instance.status] ?? "bg-slate-100 text-slate-500"
                }`}
              >
                {instance.status}
              </span>
              <span className="text-[11px] text-slate-400">
                {instance.proposer_sub} · {instance.channel}
              </span>
              {instance.correction_id != null && (
                <span className="font-mono text-[11px] text-emerald-700">
                  correction #{instance.correction_id}
                </span>
              )}
              {instance.status === "awaiting_confirmation" && (
                <span className="ml-auto flex gap-1.5">
                  <button
                    type="button"
                    data-testid={`domain-action-approve-${instance.id}`}
                    disabled={busy}
                    onClick={() => void confirmInstance(instance, true)}
                    className="rounded bg-emerald-600 px-2 py-0.5 text-[11px] text-white disabled:opacity-50"
                  >
                    {t("console.entities.actions.confirm")}
                  </button>
                  <button
                    type="button"
                    data-testid={`domain-action-reject-${instance.id}`}
                    disabled={busy}
                    onClick={() => void confirmInstance(instance, false)}
                    className="rounded border border-rose-300 px-2 py-0.5 text-[11px] text-rose-700 disabled:opacity-50"
                  >
                    {t("console.entities.actions.reject")}
                  </button>
                </span>
              )}
            </div>
            {instance.error && (
              <p className="mt-0.5 text-[11px] text-rose-600">{instance.error}</p>
            )}
          </div>
        ))}
        {instances.length === 0 && (
          <p className="mt-1 text-xs text-slate-400">
            {t("console.entities.actions.noInstances")}
          </p>
        )}
      </div>
    </div>
  );
}
