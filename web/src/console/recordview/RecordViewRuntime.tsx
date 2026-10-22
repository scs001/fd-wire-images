// 单记录页运行时（wire-record-view 任务 4.2/5.1/5.2）——公开扫码页与
// console 试预览共用（同一取数组装逻辑的同构渲染面）。
//
// props {endpoint}：公开态调 GET /api/v1/share/record-view/{id}?t=&k=（裸
// fetch 绝不带 Authorization——免登录扫码接收者直接打开；404=链接无效统一
// 口径、503=分享未配置、429=令牌限流、found=false=「无此记录」200 态）；
// 试预览态调 POST /artifacts/{id}/record-view/preview（console 会话身份，
// 错误 detail 如实呈现——预览是构建工具不适用「不泄露」纪律）。四态框架
// （加载/就绪/无此记录/无效链接）由本组件承载，品牌顶栏/页脚/noindex 在
// ShareView 层常驻（四态同一品牌面）。
//
// 渲染移动优先（扫码场景）：header 字段区（label: 值）+ 时间轴（sections
// 按 dateColumn 升序渲染节点——sortRowsByDate 幂等防线；节点点击展开该行
// 全部列值；truncated 段标「已截断」；error 段标可读错误不静默空段）。
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  apiErrorText,
  ConsoleApiError,
  fetchSharedRecordView,
  previewRecordView,
} from "../api";
import {
  recordCellText,
  sortRowsByDate,
  type RecordViewPayload,
} from "./recordViewSpec";

/** 取数目标：公开态（扫码分享页）/ 试预览态（console 构建器内）。 */
export type RecordViewRuntimeEndpoint =
  | { kind: "public"; artifactId: number; token: string; recordKey: string }
  | { kind: "preview"; artifactId: number; recordKey: string };

/** 运行时四态 + 限流态（加载/就绪/无此记录/链接无效/限流）。 */
type RuntimeState =
  | { kind: "loading" }
  | { kind: "ready"; payload: RecordViewPayload }
  | { kind: "notFound" }
  | { kind: "invalid" }
  | { kind: "rateLimited"; message: string }
  | { kind: "disabled" }
  | { kind: "previewError"; message: string };

export function RecordViewRuntime({
  endpoint,
  onLoaded,
}: {
  endpoint: RecordViewRuntimeEndpoint;
  /** 就绪回调（公开页 ShareView 抬升 title/version 供页脚/og/docTitle）。 */
  onLoaded?: (meta: { title: string; version: number }) => void;
}) {
  const { t } = useTranslation();
  const [state, setState] = useState<RuntimeState>({ kind: "loading" });

  const endpointKey =
    endpoint.kind === "public"
      ? `public:${endpoint.artifactId}:${endpoint.token}:${endpoint.recordKey}`
      : `preview:${endpoint.artifactId}:${endpoint.recordKey}`;

  useEffect(() => {
    let alive = true;
    setState({ kind: "loading" });
    const ready = (payload: RecordViewPayload) => {
      if (!alive) return;
      if (payload.found) {
        setState({ kind: "ready", payload });
        onLoaded?.({ title: payload.title, version: payload.version });
      } else {
        // 「无此记录」态（200 非错误）：如实呈现，不泄露是否存在过。
        setState({ kind: "notFound" });
      }
    };
    if (endpoint.kind === "public") {
      fetchSharedRecordView(
        endpoint.artifactId,
        endpoint.token,
        endpoint.recordKey,
      )
        .then(ready)
        .catch((err: unknown) => {
          if (!alive) return;
          const status = err instanceof ConsoleApiError ? err.status : undefined;
          if (status === 503) setState({ kind: "disabled" });
          else if (status === 429) {
            setState({
              kind: "rateLimited",
              message: apiErrorText(err, t("console.recordview.runtime.limited")),
            });
          } else {
            // 404/网络错等统一「链接无效」页——不区分原因，绝不泄露验签细节。
            setState({ kind: "invalid" });
          }
        });
    } else {
      previewRecordView(endpoint.artifactId, endpoint.recordKey)
        .then(ready)
        .catch((err: unknown) => {
          if (!alive) return;
          // 试预览是 console 构建工具：服务端 detail（422 spec 非法等）如实
          // 呈现，帮助定位配置问题。
          setState({
            kind: "previewError",
            message: apiErrorText(err, t("console.recordview.preview.failed")),
          });
        });
    }
    return () => {
      alive = false;
    };
    // endpointKey 序列化依赖（endpoint 对象逐 prop 展开会漏 onLoaded 重建）。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [endpointKey, t]);

  if (state.kind === "loading") {
    return (
      <p
        data-testid="record-view-loading"
        className="mt-6 text-sm text-slate-400"
      >
        {t("console.recordview.runtime.loading")}
      </p>
    );
  }

  if (state.kind === "notFound") {
    return (
      <div
        data-testid="record-view-notfound"
        className="mx-auto mt-16 max-w-md rounded-lg border border-slate-200 bg-white p-8 text-center"
      >
        <h1 className="text-lg font-semibold text-slate-800">
          {t("console.recordview.runtime.notFoundTitle")}
        </h1>
        <p className="mt-2 text-sm text-slate-500">
          {t("console.recordview.runtime.notFoundBody")}
        </p>
      </div>
    );
  }

  if (state.kind === "invalid") {
    return (
      <div
        data-testid="record-view-invalid"
        className="mx-auto mt-16 max-w-md rounded-lg border border-slate-200 bg-white p-8 text-center"
      >
        <h1 className="text-lg font-semibold text-slate-800">
          {t("console.dashboard.share.invalidTitle")}
        </h1>
        <p className="mt-2 text-sm text-slate-500">
          {t("console.dashboard.share.invalidBody")}
        </p>
      </div>
    );
  }

  if (state.kind === "disabled") {
    return (
      <div
        data-testid="record-view-disabled"
        className="mx-auto mt-16 max-w-md rounded-lg border border-slate-200 bg-white p-8 text-center"
      >
        <h1 className="text-lg font-semibold text-slate-800">
          {t("console.dashboard.share.disabledTitle")}
        </h1>
        <p className="mt-2 text-sm text-slate-500">
          {t("console.dashboard.share.disabledBody")}
        </p>
      </div>
    );
  }

  if (state.kind === "rateLimited") {
    return (
      <div
        data-testid="record-view-rate-limited"
        className="mx-auto mt-16 max-w-md rounded-lg border border-slate-200 bg-white p-8 text-center"
      >
        <h1 className="text-lg font-semibold text-slate-800">
          {t("console.recordview.runtime.limitedTitle")}
        </h1>
        <p className="mt-2 text-sm text-slate-500">{state.message}</p>
      </div>
    );
  }

  if (state.kind === "previewError") {
    return (
      <div
        data-testid="record-view-preview-error"
        className="mt-4 rounded-md border border-red-200 bg-red-50 p-4"
      >
        <p className="text-xs text-red-700">{state.message}</p>
      </div>
    );
  }

  return (
    <RecordViewBody
      key={endpointKey}
      payload={state.payload}
    />
  );
}

// ── 就绪态渲染（header 字段区 + 时间轴）────────────────────────────────

function RecordViewBody({ payload }: { payload: RecordViewPayload }) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState<string | null>(null);

  return (
    <div data-testid="record-view-body" className="mt-2 space-y-6">
      {/* header 字段区：label: 值（值原样 String()，空值占位 —）。 */}
      {payload.header.length > 0 && (
        <dl
          data-testid="record-view-header"
          className="grid grid-cols-1 gap-x-6 gap-y-2 sm:grid-cols-2"
        >
          {payload.header.map((field) => (
            <div
              key={field.column}
              data-testid="record-view-header-field"
              data-column={field.column}
              className="flex items-baseline gap-2 border-b border-slate-100 pb-1.5"
            >
              <dt className="shrink-0 text-xs text-slate-400">{field.label}</dt>
              <dd className="min-w-0 break-words text-sm text-slate-800">
                {recordCellText(field.value)}
              </dd>
            </div>
          ))}
        </dl>
      )}

      {/* 时间轴：逐 section 按 dateColumn 升序渲染节点（移动优先单列）。 */}
      <div data-testid="record-view-timeline" className="space-y-6">
        {payload.sections.map((section, sectionIndex) => (
          <section
            key={`${sectionIndex}-${section.title}`}
            data-testid="record-view-section"
            data-title={section.title}
          >
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-sm font-semibold text-slate-700">
                {section.title}
              </h3>
              {section.truncated && (
                <span
                  data-testid="record-view-truncated"
                  className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-700"
                >
                  {t("console.recordview.runtime.truncated")}
                </span>
              )}
              {section.rows.length === 0 && !section.error && (
                <span className="text-xs text-slate-400">
                  {t("console.recordview.runtime.sectionEmpty")}
                </span>
              )}
            </div>

            {section.error ? (
              // 引用失效如实（不静默空段）：可读错误标注。
              <p
                data-testid="record-view-section-error"
                className="mt-2 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800"
              >
                {section.error}
              </p>
            ) : (
              <ol className="mt-2 space-y-0 border-l border-slate-200 pl-0">
                {sortRowsByDate(section.rows, section.dateColumn).map(
                  (row, rowIndex) => {
                    const nodeKey = `${sectionIndex}-${rowIndex}`;
                    const open = expanded === nodeKey;
                    const entries = Object.entries(row).filter(
                      ([column]) => column !== "__row_id",
                    );
                    return (
                      <li key={nodeKey} data-testid="record-view-node" className="relative">
                        {/* 竖线节点圆点 + 日期主行（点击展开该行全部列值）。 */}
                        <button
                          type="button"
                          data-testid="record-view-node-toggle"
                          aria-expanded={open}
                          onClick={() => setExpanded(open ? null : nodeKey)}
                          className="flex w-full items-center gap-2 py-2 pl-4 text-left"
                        >
                          <span
                            aria-hidden
                            className="absolute left-[-4.5px] top-[18px] h-2 w-2 rounded-full bg-slate-300 ring-2 ring-white"
                          />
                          <span className="font-mono text-sm text-slate-700">
                            {recordCellText(row[section.dateColumn])}
                          </span>
                          <span className="ml-auto text-[10px] text-slate-400">
                            {open
                              ? t("console.recordview.runtime.collapse")
                              : t("console.recordview.runtime.expand")}
                          </span>
                        </button>
                        {open && (
                          <dl
                            data-testid="record-view-node-detail"
                            className="ml-4 mb-2 rounded-md border border-slate-100 bg-slate-50/70 p-3"
                          >
                            {entries.map(([column, value]) => (
                              <div
                                key={column}
                                className="flex items-baseline gap-2 border-b border-slate-100 py-1 last:border-0"
                              >
                                <dt className="shrink-0 text-[11px] text-slate-400">
                                  {column}
                                </dt>
                                <dd className="min-w-0 break-words text-xs text-slate-700">
                                  {recordCellText(value)}
                                </dd>
                              </div>
                            ))}
                          </dl>
                        )}
                      </li>
                    );
                  },
                )}
              </ol>
            )}
          </section>
        ))}
      </div>
    </div>
  );
}
