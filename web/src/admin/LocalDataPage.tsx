// 客户本地数据运营视图（#/admin/localdata，wire-customer-local-data 任务 5.1）：
// per-cell 客户数据集存储用量 + 连接器同步健康（GET /internal/datasets/overview）。
// 规格红线：**只读**——归入既有用量视图职能；吊销/删除/白名单修改等管理操作
// 只在客户控制台自助，本页不渲染任何管理入口（正常态零按钮，测试有断言）。
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { AdminErrorBox, AdminStates } from "./AdminLayout";
import {
  fetchCustomerLocalDataOverview,
  type CustomerLocalDataOverview,
  type DatasetConnectorHealth,
} from "./api";

/** 连接器状态徽标配色（online/offline 由 api 侧按心跳推导）。 */
const CONNECTOR_STATUS_CLASS: Record<string, string> = {
  online: "border-emerald-200 bg-emerald-50 text-emerald-700",
  offline: "border-slate-200 bg-slate-100 text-slate-500",
};

const KB = 1024;
const MB = 1024 * KB;
const GB = 1024 * MB;
const TB = 1024 * GB;

/** 存储用量人性化（B/KB/MB/GB/TB，一位小数）；title 保留原始字节数。 */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < KB) return `${bytes} B`;
  if (bytes < MB) return `${(bytes / KB).toFixed(1)} KB`;
  if (bytes < GB) return `${(bytes / MB).toFixed(1)} MB`;
  if (bytes < TB) return `${(bytes / GB).toFixed(1)} GB`;
  return `${(bytes / TB).toFixed(1)} TB`;
}

export function LocalDataPage() {
  const { t } = useTranslation();
  const [overview, setOverview] = useState<CustomerLocalDataOverview[] | null>(
    null,
  );
  const [error, setError] = useState<{
    status?: number;
    message: string;
    detail?: string;
  } | null>(null);

  const load = useCallback(async () => {
    setError(null);
    setOverview(null);
    try {
      const data = await fetchCustomerLocalDataOverview();
      setOverview(data);
    } catch (err) {
      setError(err as { status?: number; message: string });
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold">{t("admin.localData.title")}</h1>
          <p className="mt-1 max-w-2xl text-xs text-slate-500">
            {t("admin.localData.subtitle")}
          </p>
        </div>
        <span
          data-testid="admin-localdata-readonly-note"
          className="rounded-md border border-slate-200 bg-slate-50 px-2 py-1 text-[11px] text-slate-500"
        >
          {t("admin.localData.readonlyNote")}
        </span>
      </div>

      {error && <AdminErrorBox error={error} onRetry={() => void load()} />}

      <AdminStates
        loading={overview == null && !error}
        empty={overview != null && overview.length === 0}
        emptyTestId="admin-localdata-empty"
      >
        {overview && (
          <table
            data-testid="admin-localdata-table"
            className="mt-4 w-full border-collapse text-sm"
          >
            <thead>
              <tr className="border-b border-slate-200 text-left text-xs text-slate-400">
                <th className="py-2 pr-4">{t("admin.localData.colCustomer")}</th>
                <th className="py-2 pr-4 text-right">
                  {t("admin.localData.colDatasets")}
                </th>
                <th className="py-2 pr-4 text-right">
                  {t("admin.localData.colStorage")}
                </th>
                <th className="py-2 pr-4">
                  {t("admin.localData.colConnectors")}
                </th>
              </tr>
            </thead>
            <tbody>
              {overview.map((row) => (
                <tr
                  key={row.customer_id}
                  data-testid="admin-localdata-row"
                  className="border-b border-slate-100 align-top"
                >
                  <td
                    data-testid="admin-localdata-customer"
                    className="py-3 pr-4 font-mono text-xs text-slate-700"
                  >
                    {row.customer_id}
                  </td>
                  <td className="py-3 pr-4 text-right font-mono text-xs">
                    <span data-testid="admin-localdata-datasets">
                      {row.datasets.toLocaleString()}
                    </span>{" "}
                    <span className="text-slate-400">
                      {t("admin.localData.datasetsUnit")}
                    </span>
                  </td>
                  <td className="py-3 pr-4 text-right font-mono text-xs">
                    <span
                      data-testid="admin-localdata-storage"
                      title={String(row.storage_bytes)}
                    >
                      {formatBytes(row.storage_bytes)}
                    </span>
                  </td>
                  <td className="py-3 pr-4">
                    {row.connectors.length === 0 ? (
                      <span
                        data-testid="admin-localdata-no-connectors"
                        className="text-xs text-slate-400"
                      >
                        {t("admin.localData.noConnectors")}
                      </span>
                    ) : (
                      <div
                        data-testid="admin-localdata-connectors"
                        className="flex flex-col gap-1"
                      >
                        {row.connectors.map((connector) => (
                          <ConnectorLine
                            key={connector.id}
                            connector={connector}
                          />
                        ))}
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </AdminStates>
    </div>
  );
}

function ConnectorLine({ connector }: { connector: DatasetConnectorHealth }) {
  const { t } = useTranslation();
  const statusKey = `admin.localData.connectorStatus.${connector.status}`;
  return (
    <div
      data-testid="admin-localdata-connector"
      className="flex flex-wrap items-center gap-2 text-xs"
    >
      <span className="font-mono text-[11px] text-slate-600">
        {connector.id}
      </span>
      <span
        data-testid="admin-localdata-connector-status"
        className={`rounded border px-1.5 py-0.5 text-[11px] ${
          CONNECTOR_STATUS_CLASS[connector.status] ??
          "border-slate-200 bg-slate-50 text-slate-500"
        }`}
      >
        {t(statusKey, { defaultValue: connector.status })}
      </span>
      <span
        data-testid="admin-localdata-connector-heartbeat"
        className="font-mono text-[11px] text-slate-400"
      >
        {connector.last_heartbeat_at ?? "—"}
      </span>
    </div>
  );
}
