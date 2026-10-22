// 目录治理页（#/admin/catalog）：registered 镜像列表 + 审核意见录入/留档。
// 如实标注：镜像仅同步管道可写——状态修改在权威库侧完成，本页只看不改、
// 意见只追加（append-only）到 admin 自己的 catalog_review_notes。
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { AdminErrorBox, AdminStates } from "./AdminLayout";
import {
  addVerifyNote,
  CATALOG_PENDING_MAX_LIMIT,
  fetchCatalogPending,
  fetchReviewNotes,
  type IndicatorPendingItem,
  type ReviewNote,
} from "./api";

type MirrorStatus = "registered" | "verified";

/** 目录治理单页条数（与后端 limit 上限同值；规模现实见 api.ts 注释）。 */
const PAGE_SIZE = CATALOG_PENDING_MAX_LIMIT;

export function CatalogGovernancePage() {
  const { t } = useTranslation();
  const [status, setStatus] = useState<MirrorStatus>("registered");
  // 分页（wire-admin-console-routing）：镜像规模实测 verified 3,421 条 /
  // registered 54,132 条——全量拉取是一次性渲染白屏的直接成因，故只取本页。
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [items, setItems] = useState<IndicatorPendingItem[] | null>(null);
  const [error, setError] = useState<{ status?: number; message: string; detail?: string } | null>(null);
  const [notesByIndicator, setNotesByIndicator] = useState<Record<string, ReviewNote[]>>({});
  const [openId, setOpenId] = useState<string | null>(null);

  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const load = useCallback(async () => {
    setError(null);
    setItems(null);
    try {
      const response = await fetchCatalogPending(status, {
        limit: PAGE_SIZE,
        offset: (page - 1) * PAGE_SIZE,
      });
      setItems(response.items);
      // total 是状态匹配总数（非本页条数），页数由它算出，无需额外计数请求。
      setTotal(response.total);
    } catch (err) {
      setError(err as { status?: number; message: string });
    }
  }, [status, page]);

  useEffect(() => {
    void load();
  }, [load]);

  const toggleNotes = async (indicatorId: string) => {
    if (openId === indicatorId) {
      setOpenId(null);
      return;
    }
    setOpenId(indicatorId);
    try {
      const notes = await fetchReviewNotes(indicatorId);
      setNotesByIndicator((prev) => ({ ...prev, [indicatorId]: notes }));
    } catch (err) {
      setError(err as { status?: number; message: string });
    }
  };

  const refreshNotes = async (indicatorId: string) => {
    const notes = await fetchReviewNotes(indicatorId);
    setNotesByIndicator((prev) => ({ ...prev, [indicatorId]: notes }));
  };

  return (
    <div>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold">{t("admin.catalog.title")}</h1>
          <p className="mt-1 max-w-2xl text-xs text-slate-500">
            {t("admin.catalog.subtitle")}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <label htmlFor="catalog-status" className="text-xs text-slate-500">
            {t("admin.catalog.statusFilter")}
          </label>
          <select
            id="catalog-status"
            data-testid="catalog-status-filter"
            value={status}
            onChange={(e) => {
              setStatus(e.target.value as MirrorStatus);
              // 换状态即回到第 1 页：另一状态的页数与当前页码无关，留在原
              // 页码会得到越界空页（total 变小则 items 为空）。
              setPage(1);
            }}
            className="rounded-md border border-slate-300 px-2 py-1 text-sm"
          >
            <option value="registered">{t("admin.catalog.registered")}</option>
            <option value="verified">{t("admin.catalog.verified")}</option>
          </select>
        </div>
      </div>

      <p
        data-testid="admin-catalog-readonly-note"
        className="mt-4 rounded-md border border-amber-200 bg-amber-50 px-4 py-2.5 text-xs text-amber-700"
      >
        {t("admin.catalog.readonlyNote")}
      </p>

      {error && <AdminErrorBox error={error} onRetry={() => void load()} />}

      <AdminStates
        loading={items == null && !error}
        empty={(items?.length ?? 0) === 0 && !error}
        emptyTestId="admin-catalog-empty"
      >
        {items != null && items.length > 0 && (
          <ul data-testid="admin-catalog-list" className="mt-4 space-y-2">
          {items!.map((item) => (
            <li
              key={item.indicator_id}
              data-testid="admin-catalog-item"
              className="rounded-lg border border-slate-200 bg-white p-4"
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-xs text-slate-400">{item.code}</span>
                <span className="text-sm font-medium text-slate-800">
                  {item.name_zh ?? item.name_en ?? item.indicator_id}
                </span>
                <span
                  data-testid="admin-catalog-status-badge"
                  className={`rounded-full px-2 py-0.5 text-xs ring-1 ${
                    item.status === "registered"
                      ? "bg-amber-50 text-amber-700 ring-amber-200"
                      : "bg-emerald-50 text-emerald-700 ring-emerald-200"
                  }`}
                >
                  {item.status === "registered"
                    ? t("admin.catalog.registered")
                    : t("admin.catalog.verified")}
                </span>
                {item.domain && (
                  <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[11px] text-slate-500">
                    {item.domain}
                  </span>
                )}
                <button
                  type="button"
                  data-testid="admin-catalog-notes-toggle"
                  onClick={() => void toggleNotes(item.indicator_id)}
                  className="ml-auto rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50"
                >
                  {t("admin.catalog.notesToggle", {
                    count: notesByIndicator[item.indicator_id]?.length ?? 0,
                  })}
                </button>
              </div>
              {openId === item.indicator_id && (
                <div
                  data-testid="admin-catalog-notes-panel"
                  className="mt-3 rounded-md border border-slate-200 bg-slate-50/60 p-4"
                >
                  <ul className="space-y-1">
                    {(notesByIndicator[item.indicator_id] ?? []).map((note) => (
                      <li
                        key={note.id}
                        data-testid="admin-catalog-note-item"
                        className="text-xs text-slate-600"
                      >
                        <span className="font-mono text-[11px] text-slate-400">
                          {note.author} · {note.created_at.slice(0, 16).replace("T", " ")}
                        </span>
                        {" — "}
                        {note.note}
                      </li>
                    ))}
                    {(notesByIndicator[item.indicator_id]?.length ?? 0) === 0 && (
                      <li className="text-xs text-slate-400">
                        {t("admin.catalog.noNotes")}
                      </li>
                    )}
                  </ul>
                  <NoteInput
                    indicatorId={item.indicator_id}
                    onSaved={() => void refreshNotes(item.indicator_id)}
                    onError={(err) => setError(err)}
                  />
                </div>
              )}
            </li>
          ))}
          </ul>
        )}
      </AdminStates>
      {items != null && items.length > 0 && total > PAGE_SIZE && (
        <div
          data-testid="admin-catalog-pagination"
          className="mt-3 flex flex-wrap items-center gap-3 text-xs text-slate-500"
        >
          <span data-testid="admin-catalog-pagination-info">
            {t("admin.catalog.pagination", { total, page, pages })}
          </span>
          <button
            type="button"
            data-testid="admin-catalog-prev-page"
            disabled={page <= 1}
            onClick={() => setPage((current) => Math.max(1, current - 1))}
            className="rounded border border-slate-300 px-2 py-1 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {t("admin.catalog.prev")}
          </button>
          <button
            type="button"
            data-testid="admin-catalog-next-page"
            disabled={page >= pages}
            onClick={() => setPage((current) => current + 1)}
            className="rounded border border-slate-300 px-2 py-1 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {t("admin.catalog.next")}
          </button>
        </div>
      )}
    </div>
  );
}

function NoteInput({
  indicatorId,
  onSaved,
  onError,
}: {
  indicatorId: string;
  onSaved: () => void;
  onError: (err: { status?: number; message: string }) => void;
}) {
  const { t } = useTranslation();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!text.trim()) return;
    setBusy(true);
    try {
      await addVerifyNote(indicatorId, text.trim());
      setText("");
      onSaved();
    } catch (err) {
      onError(err as { status?: number; message: string });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mt-3 flex flex-wrap items-center gap-2">
      <input
        data-testid="admin-catalog-note-input"
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder={t("admin.catalog.notePlaceholder")}
        className="w-72 rounded-md border border-slate-300 px-2 py-1.5 text-sm"
      />
      <button
        type="button"
        data-testid="admin-catalog-note-submit"
        disabled={busy || !text.trim()}
        onClick={() => void submit()}
        className="rounded-md bg-slate-900 px-3 py-1.5 text-xs text-white hover:bg-slate-700 disabled:opacity-50"
      >
        {busy ? t("admin.common.working") : t("admin.catalog.noteSubmit")}
      </button>
    </div>
  );
}
