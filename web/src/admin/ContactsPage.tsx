// 商留通道页（#/admin/contacts，add-contact-intake 任务 1.4）：官网
// 「联系开通」表单提交的处置面——时间倒序列表 + 未读徽标 + 标记已读。
// 红线（spec contact-intake）：来源 IP 只以哈希留档，本页任何字段都不呈现
// 来源信息；标记已读是唯一写动作（无删除/编辑面）。
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { AdminErrorBox, AdminStates } from "./AdminLayout";
import {
  fetchContactMessages,
  markContactMessageRead,
  type ContactMessage,
} from "./api";

/** 单页条数（与后端默认 50 同值；limit 上限 200 由后端收敛）。 */
const PAGE_SIZE = 50;

export function ContactsPage() {
  const { t } = useTranslation();
  const [items, setItems] = useState<ContactMessage[] | null>(null);
  const [total, setTotal] = useState(0);
  const [unread, setUnread] = useState(0);
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState<
    { status?: number; message: string; detail?: string } | null
  >(null);
  const [markingId, setMarkingId] = useState<number | null>(null);
  const [marked, setMarked] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    setItems(null);
    try {
      const page = await fetchContactMessages({ limit: PAGE_SIZE, offset });
      setItems(page.items);
      setTotal(page.total);
      setUnread(page.unread);
    } catch (err) {
      setError(err as { status?: number; message: string });
    }
  }, [offset]);

  useEffect(() => {
    void load();
  }, [load]);

  const markRead = async (id: number) => {
    setMarkingId(id);
    setError(null);
    try {
      await markContactMessageRead(id);
      setMarked(true);
      await load();
    } catch (err) {
      setError(err as { status?: number; message: string });
    } finally {
      setMarkingId(null);
    }
  };

  const from = total === 0 ? 0 : offset + 1;
  const to = Math.min(offset + PAGE_SIZE, total);

  return (
    <div>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold">{t("admin.contacts.title")}</h1>
          <p className="mt-1 max-w-2xl text-xs text-slate-500">
            {t("admin.contacts.subtitle")}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {unread > 0 && (
            <span
              data-testid="admin-contacts-unread-badge"
              className="rounded-full bg-rose-100 px-2.5 py-1 text-xs font-semibold text-rose-700 ring-1 ring-rose-200"
            >
              {t("admin.contacts.unreadBadge", { count: unread })}
            </span>
          )}
          <span className="text-xs text-slate-400">
            {t("admin.contacts.pageInfo", { from, to, total })}
          </span>
        </div>
      </div>

      {marked && (
        <p
          data-testid="admin-contacts-marked"
          className="mt-3 text-xs text-emerald-700"
        >
          {t("admin.contacts.marked")}
        </p>
      )}

      {error && <AdminErrorBox error={error} onRetry={() => void load()} />}

      <AdminStates
        loading={items == null && !error}
        empty={(items?.length ?? 0) === 0 && !error}
        emptyTestId="admin-contacts-empty"
      >
        {items != null && items.length > 0 && (
          <table
            data-testid="admin-contacts-table"
            className="mt-4 w-full border-collapse text-sm"
          >
            <thead>
              <tr className="border-b border-slate-200 text-left text-xs text-slate-400">
                <th className="py-2 pr-4">#</th>
                <th className="py-2 pr-4">{t("admin.contacts.colTime")}</th>
                <th className="py-2 pr-4">{t("admin.contacts.colName")}</th>
                <th className="py-2 pr-4">{t("admin.contacts.colEmail")}</th>
                <th className="py-2 pr-4">{t("admin.contacts.colCompany")}</th>
                <th className="py-2 pr-4">{t("admin.contacts.colMessage")}</th>
                <th className="py-2 pr-4">{t("admin.contacts.colStatus")}</th>
                <th className="py-2 pr-4">{t("admin.contacts.colAction")}</th>
              </tr>
            </thead>
            <tbody>
              {items!.map((msg) => (
                <tr
                  key={msg.id}
                  data-testid="admin-contacts-row"
                  className="border-b border-slate-100"
                >
                  <td className="py-2 pr-4 text-xs text-slate-400">{msg.id}</td>
                  <td className="py-2 pr-4 font-mono text-xs">
                    {msg.created_at.replace("T", " ").slice(0, 16)}
                  </td>
                  <td className="py-2 pr-4">
                    <span data-testid="admin-contacts-name">{msg.name}</span>
                  </td>
                  <td className="py-2 pr-4 font-mono text-xs">
                    <span data-testid="admin-contacts-email">{msg.email}</span>
                  </td>
                  <td className="py-2 pr-4 text-xs text-slate-600">
                    {msg.company ?? "—"}
                  </td>
                  <td className="max-w-[16rem] py-2 pr-4 text-xs text-slate-600">
                    <span
                      data-testid="admin-contacts-message"
                      className="line-clamp-3 whitespace-pre-wrap"
                      title={msg.message}
                    >
                      {msg.message}
                    </span>
                  </td>
                  <td className="py-2 pr-4">
                    {msg.status === "new" ? (
                      <span
                        data-testid={`admin-contacts-status-${msg.id}`}
                        className="rounded-full bg-sky-50 px-2 py-0.5 text-xs text-sky-700 ring-1 ring-sky-200"
                      >
                        {t("admin.contacts.status.new")}
                      </span>
                    ) : (
                      <span
                        data-testid={`admin-contacts-status-${msg.id}`}
                        className="rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-500 ring-1 ring-slate-200"
                      >
                        {t("admin.contacts.status.read")}
                      </span>
                    )}
                  </td>
                  <td className="py-2 pr-4">
                    {msg.status === "new" ? (
                      <button
                        type="button"
                        data-testid="contact-mark-read"
                        disabled={markingId != null}
                        onClick={() => void markRead(msg.id)}
                        className="rounded-md border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-100 disabled:opacity-50"
                      >
                        {t("admin.contacts.markRead")}
                      </button>
                    ) : (
                      <span className="text-xs text-slate-300">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {items != null && items.length > 0 && (
          <p className="mt-2 text-[11px] text-slate-400">
            {t("admin.contacts.ipHashNote")}
          </p>
        )}
        {total > PAGE_SIZE && (
          <div className="mt-3 flex items-center gap-3">
            <button
              type="button"
              data-testid="admin-contacts-prev"
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              className="rounded-md border border-slate-300 px-3 py-1 text-xs text-slate-600 hover:bg-slate-100 disabled:opacity-40"
            >
              {t("admin.contacts.prev")}
            </button>
            <button
              type="button"
              data-testid="admin-contacts-next"
              disabled={to >= total}
              onClick={() => setOffset(offset + PAGE_SIZE)}
              className="rounded-md border border-slate-300 px-3 py-1 text-xs text-slate-600 hover:bg-slate-100 disabled:opacity-40"
            >
              {t("admin.contacts.next")}
            </button>
          </div>
        )}
      </AdminStates>
    </div>
  );
}
