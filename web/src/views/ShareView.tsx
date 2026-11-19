// 公开分享页（/#/share/dashboard/{id}?t=&v=，wire-dashboard-share 任务 2.1；
// wire-form-builder 任务 4.5 起 /#/share/form/{id}?t=&v=、wire-rich-text-docs
// 任务 5.2 起 /#/share/doc/{id}?t=&v= 同框架分派）。
// - 免登录只读：调公开端点 GET /api/v1/share/{dashboard|form|doc}/{id}?t=&v=
//   （无 Bearer）；dashboard 态 → parseDashboardSpec 宽容解析标题 +
//   DashboardRenderer 复用渲染；form 态 → parseFormSpec + FormRuntime
//   （public endpoint，t/v 透传进 submit 地址——提交成功/失败态在组件内
//   呈现，公开提交免登录且绝不携带 Authorization）；doc 态 → MarkdownView
//   渲染钉死版本正文（「渲染安全底线」对编辑预览与公开分享页同样适用——
//   零 innerHTML 的同一渲染器，正文里的 <script> 只会是文本）。
// - 品牌面（wire-share-branding）：四态同一顶栏双语品牌锁定（寻数·柏讯 /
//   Wire，双语锁定即两语言下文案相同）链产品首页；页脚就绪态
//   「{标题} · 由 寻数·柏讯 提供 · 版本 v{n}」、非就绪态产品署名；
//   就绪态注入 og:title/og:description/og:site_name 链接预览元信息。
// - noindex 红线：挂载即注入 <meta name="robots" content="noindex">（卸载
//   移除）——分享页不得入公开目录或搜索引擎；document.title 前缀「分享 · 」。
// - 失败口径与 api 对齐且不泄露原因差异：404/网络错 →「链接无效或已过期」
//   静态页（无重试按钮——重试也只会再 404）；503（未配置 share secret）→
//   「分享功能未开通」页。
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  ConsoleApiError,
  fetchSharedDashboard,
  fetchSharedForm,
  sharedFormOptionsUrl,
  sharedFormSubmitUrl,
} from "../console/api";
import { API_BASE } from "../catalog/api";
import { DashboardRenderer } from "../console/dashboard/DashboardRenderer";
import { parseDashboardSpec } from "../console/dashboard/spec";
import { MarkdownView } from "../console/doc/md";
import { FormRuntime } from "../console/form/FormRuntime";
import { parseFormSpec } from "../console/form/formSpec";
import { RecordViewRuntime } from "../console/recordview/RecordViewRuntime";

/** 分享模式：按路由路径分派（dashboard=只读看板；form=可填写表单；
 *  record-view=单记录公开页——wire-record-view 任务 5.1 第三 mode；
 *  doc=只读正文页——wire-rich-text-docs 任务 5.2 第四 mode）。 */
export type ShareMode = "dashboard" | "form" | "record-view" | "doc";

/** doc 公开端点载荷（GET /api/v1/share/doc/{id}?t&v）。expires_at 0 档
 *  （不设期·印刷物）为 null——分享页不展示到期时间，字段仅为契约完整。 */
interface SharedDoc {
  title: string;
  /** 产物信封原样 {title, spec:{format:"markdown", text}}。 */
  spec: Record<string, unknown>;
  version: number;
  expires_at: string | null;
}

/** 公开取分享文档（无鉴权端点——绝不携带登录头，未登录接收者直接打开）。
 *  与 fetchSharedDashboard 同构就地实现（api.ts 是并行改动面，分享页不
 *  依赖其新增导出）：同一 API_BASE、同一错误语义——404=链接无效/过期/
 *  产物下线（服务端统一口径，不区分原因）；503=分享功能未配置。 */
async function fetchSharedDoc(
  id: number,
  token: string,
  version: number | null,
): Promise<SharedDoc> {
  const params = new URLSearchParams({ t: token });
  if (version != null) params.set("v", String(version));
  const url = `${API_BASE}/v1/share/doc/${id}?${params}`;
  let response: Response;
  try {
    response = await fetch(url, {
      method: "GET",
      headers: { Accept: "application/json" },
    });
  } catch (err) {
    if (err instanceof ConsoleApiError) throw err;
    throw new ConsoleApiError(
      `network error fetching ${url}: ${(err as Error).message}`,
    );
  }
  if (!response.ok) {
    // detail 不解析：无效页文案与失败原因无关（不泄露验签细节），503 由
    // 状态码分派到「分享功能未开通」——与 api.ts readResponse 行为等价。
    throw new ConsoleApiError(
      `console api ${response.status} for ${url}`,
      response.status,
    );
  }
  return (await response.json()) as SharedDoc;
}

/** doc 钉死版本信封正文抽取：{title, spec:{format, text}} → text。宽容
 *  读取（非字符串/缺席回空串渲染空文档而非崩页——服务端已保证形状，此处
 *  只是前端防御位，绝不因此伪造内容）。 */
function docTextOf(envelope: Record<string, unknown>): string {
  const inner = envelope.spec;
  if (typeof inner !== "object" || inner === null) return "";
  const text = (inner as Record<string, unknown>).text;
  return typeof text === "string" ? text : "";
}

/** 四态：加载中 / 就绪 / 链接无效（含网络错）/ 分享未开通（503）。 */
type ShareState =
  | { kind: "loading" }
  | {
      kind: "ready";
      /** 钉死版本 spec（产物信封原样——按 mode 交给对应渲染件宽容解析）。 */
      spec: Record<string, unknown>;
      title: string;
      version: number;
    }
  | { kind: "invalid" }
  | { kind: "disabled" };

export function ShareView({
  artifactId,
  token,
  version,
  mode = "dashboard",
  recordKey = "",
}: {
  /** 路由段里的产物 id（数字字符串；非法时交给服务端 404）。 */
  artifactId: string;
  /** 签名令牌 t（缺失即空串——服务端验签必败，落无效页）。 */
  token: string;
  /** 钉死版本 v（可选；缺失时省略该参数）。 */
  version: string | null;
  /** 分享模式（路由分派；缺省看板——既有深链零漂移）。 */
  mode?: ShareMode;
  /** 键值 k（record-view 专属：扫码/输键取数；缺失即空串——服务端按
   *  键等值查零行落「无此记录」态，如实呈现）。 */
  recordKey?: string;
}) {
  const { t } = useTranslation();
  const [state, setState] = useState<ShareState>({ kind: "loading" });
  // record-view 就绪抬升面（RecordViewRuntime 拥有取数与四态，title/version
  // 上抬供页脚署名/og/docTitle；非就绪态（含「无此记录」）保持 null）。
  const [recordMeta, setRecordMeta] = useState<{
    title: string;
    version: number;
  } | null>(null);

  // noindex 红线：进入即插、离开移除（SPA 其他视图不受影响）。
  useEffect(() => {
    const meta = document.createElement("meta");
    meta.setAttribute("name", "robots");
    meta.setAttribute("content", "noindex");
    document.head.appendChild(meta);
    return () => {
      document.head.removeChild(meta);
    };
  }, []);

  // og 链接预览元信息（wire-share-branding）：仅就绪态注入，与 noindex 同
  // 手法——插/移除成对，data-share-og 标记便于清理与定位。诚实边界：这是
  // 运行时注入，只对执行 JS 的链接预览器（聊天工具内嵌浏览器等）生效；
  // 不执行 JS 的爬虫读不到（SPA 静态壳无 SSR，如需爬虫全覆盖另走入口
  // nginx 注入，见 proposal Non-Goals）。og 与 noindex 不冲突：og 服务聊天
  // 卡片预览，noindex 管不入搜索目录。
  const ogTitle =
    state.kind === "ready"
      ? state.title
      : mode === "record-view"
        ? (recordMeta?.title ?? null)
        : null;
  useEffect(() => {
    if (ogTitle == null) return;
    const ogDescription =
      mode === "form"
        ? t("console.form.ogDescription")
        : mode === "record-view"
          ? t("console.recordview.ogDescription")
          : mode === "doc"
            ? t("console.doc.ogDescription")
            : t("console.dashboard.share.ogDescription");
    const metas = (
      [
        ["og:title", ogTitle],
        ["og:description", ogDescription],
        ["og:site_name", t("console.dashboard.share.ogSiteName")],
      ] as const
    ).map(([property, content]) => {
      const meta = document.createElement("meta");
      meta.setAttribute("property", property);
      meta.setAttribute("content", content);
      meta.setAttribute("data-share-og", property);
      document.head.appendChild(meta);
      return meta;
    });
    return () => {
      metas.forEach((meta) => {
        document.head.removeChild(meta);
      });
    };
  }, [ogTitle, mode, t]);

  useEffect(() => {
    let alive = true;
    // record-view：取数与四态在 RecordViewRuntime（k 进端点、令牌验签同规），
    // 本层不走 dashboard/form 的 spec 快照 fetch——仅复位抬升面。
    if (mode === "record-view") {
      setState({ kind: "loading" });
      setRecordMeta(null);
      return () => {
        alive = false;
      };
    }
    setState({ kind: "loading" });
    const numericVersion =
      version != null && version !== "" ? Number(version) : null;
    // 标题：响应 title 优先，缺失回落 spec 宽容解析（信封/本体皆可），
    // 兜底占位按 mode 取「未命名看板/表单/文档」。
    const resolveTitle = (spec: Record<string, unknown>, rawTitle: string) => {
      // form 信封内层无标题字段——标题只认响应 title；dashboard 回落 v1
      // 解析（legacy 形态无 title 字段，回落空由兜底占位）；doc 回落信封
      // 外层 title（响应 title 已优先取钉死版本信封 title，此为双保险）。
      const parsedTitle =
        mode === "form"
          ? ""
          : mode === "doc"
            ? typeof spec.title === "string"
              ? spec.title
              : ""
            : (() => {
                const parsed = parseDashboardSpec(spec);
                return parsed.kind === "v1" ? parsed.title : "";
              })();
      return (
        (typeof rawTitle === "string" && rawTitle.trim()) ||
        parsedTitle ||
        (mode === "form"
          ? t("console.form.untitled")
          : mode === "doc"
            ? t("console.doc.untitled")
            : t("console.dashboard.share.untitled"))
      );
    };
    const ready = (spec: Record<string, unknown>, rawTitle: string, v: number) => {
      setState({
        kind: "ready",
        spec,
        title: resolveTitle(spec, rawTitle),
        version: v,
      });
    };
    if (mode === "form") {
      fetchSharedForm(Number(artifactId), token, numericVersion)
        .then((data) => {
          if (!alive) return;
          ready(data.spec, data.title, data.version);
        })
        .catch((err: unknown) => {
          if (!alive) return;
          setState(
            err instanceof ConsoleApiError && err.status === 503
              ? { kind: "disabled" }
              : { kind: "invalid" },
          );
        });
    } else if (mode === "doc") {
      // doc=静态快照读面（无取数/提交）：与 dashboard 同款 fetch + 四态；
      // 归档即失效（服务端 404）与改版不串版（服务端按 v 钉死）都在服务端，
      // 本层只如实落无效页。
      fetchSharedDoc(Number(artifactId), token, numericVersion)
        .then((data) => {
          if (!alive) return;
          ready(data.spec, data.title, data.version);
        })
        .catch((err: unknown) => {
          if (!alive) return;
          setState(
            err instanceof ConsoleApiError && err.status === 503
              ? { kind: "disabled" }
              : { kind: "invalid" },
          );
        });
    } else {
      fetchSharedDashboard(Number(artifactId), token, numericVersion)
        .then((data) => {
          if (!alive) return;
          ready(data.spec, data.title, data.version);
        })
        .catch((err: unknown) => {
          if (!alive) return;
          // 503 = 未配置 secret（分享功能整体禁用）；其余（404/网络错）一律
          // 无效链接页——不区分原因，绝不泄露验签细节。
          setState(
            err instanceof ConsoleApiError && err.status === 503
              ? { kind: "disabled" }
              : { kind: "invalid" },
          );
        });
    }
    return () => {
      alive = false;
    };
  }, [artifactId, token, version, mode, recordKey, t]);

  // document.title 前缀「分享 · 」：进页面即生效，就绪后带上标题。
  const pageTitle =
    state.kind === "ready"
      ? state.title
      : mode === "record-view" && recordMeta != null
        ? recordMeta.title
        : state.kind === "disabled"
          ? t("console.dashboard.share.disabledTitle")
          : state.kind === "invalid"
            ? t("console.dashboard.share.invalidTitle")
            : mode === "record-view"
              ? t("console.recordview.runtime.loading")
              : mode === "doc"
                ? t("console.doc.loading")
                : t("console.dashboard.share.loading");
  useEffect(() => {
    document.title = t("console.dashboard.share.docTitle", {
      title: pageTitle,
    });
  }, [pageTitle, t]);

  return (
    <div className="flex min-h-screen flex-col">
      {/* 极简顶栏：双语品牌锁定 寻数·柏讯 / Wire（无导航/登录/语言切换——
          公开只读页）。主副排版贴 App.tsx 站点顶栏品牌惯例（主名 semibold +
          副名 slate-500 小字）；双语锁定即文案不随界面语言切换；四态同一
          顶栏——invalid/disabled 页同样是产品触点。链接保持产品首页 #/。 */}
      <header
        data-testid="share-topbar"
        className="border-b border-slate-200 bg-white"
      >
        <div className="mx-auto flex max-w-5xl items-center px-4 py-3">
          <a
            href="#/"
            data-testid="share-brand"
            className="text-lg font-semibold tracking-wide"
          >
            {t("console.dashboard.share.brandNameZh")}
            <span className="ml-2 text-sm font-normal tracking-normal text-slate-500">
              {t("console.dashboard.share.brandNameEn")}
            </span>
          </a>
        </div>
      </header>

      <main className="mx-auto w-full max-w-5xl flex-1 px-4 py-8">
        {mode === "record-view" ? (
          // 单记录公开页（wire-record-view 5.1）：运行时拥有取数（裸 fetch 绝
          // 不带 Authorization）与内容四态（加载/就绪/无此记录/链接无效——
          // 复用本层品牌顶栏/页脚/noindex 框架）；就绪时上抬 title/version。
          <div data-testid="share-record-view">
            <h1
              data-testid="share-record-view-title"
              className="text-lg font-semibold text-slate-900"
            >
              {recordMeta?.title ?? t("console.recordview.untitled")}
            </h1>
            <div className="mt-4">
              <RecordViewRuntime
                key={`${artifactId}:${token}:${recordKey}`}
                endpoint={{
                  kind: "public",
                  artifactId: Number(artifactId),
                  token,
                  recordKey,
                }}
                onLoaded={(meta) => setRecordMeta(meta)}
              />
            </div>
          </div>
        ) : (
          <>
            {state.kind === "loading" && (
              <p data-testid="share-loading" className="mt-6 text-sm text-slate-400">
                {mode === "doc"
                  ? t("console.doc.loading")
                  : t("console.dashboard.share.loading")}
              </p>
            )}

            {state.kind === "invalid" && (
              <div
                data-testid="share-invalid"
                className="mx-auto mt-16 max-w-md rounded-lg border border-slate-200 bg-white p-8 text-center"
              >
                <h1 className="text-lg font-semibold text-slate-800">
                  {t("console.dashboard.share.invalidTitle")}
                </h1>
                <p className="mt-2 text-sm text-slate-500">
                  {t("console.dashboard.share.invalidBody")}
                </p>
              </div>
            )}

            {state.kind === "disabled" && (
              <div
                data-testid="share-disabled"
                className="mx-auto mt-16 max-w-md rounded-lg border border-slate-200 bg-white p-8 text-center"
              >
                <h1 className="text-lg font-semibold text-slate-800">
                  {t("console.dashboard.share.disabledTitle")}
                </h1>
                <p className="mt-2 text-sm text-slate-500">
                  {t("console.dashboard.share.disabledBody")}
                </p>
              </div>
            )}

            {state.kind === "ready" && mode === "dashboard" && (
              <div data-testid="share-dashboard">
                <h1
                  data-testid="share-dashboard-title"
                  className="text-lg font-semibold text-slate-900"
                >
                  {state.title}
                </h1>
                <div className="mt-4">
                  <DashboardRenderer spec={state.spec} />
                </div>
              </div>
            )}

            {state.kind === "ready" && mode === "form" && (
              <div data-testid="share-form">
                <h1
                  data-testid="share-form-title"
                  className="text-lg font-semibold text-slate-900"
                >
                  {state.title}
                </h1>
                <div className="mt-4">
                  <FormRuntime
                    spec={parseFormSpec(state.spec) ?? { datasetId: null, fields: [] }}
                    endpoint={{
                      kind: "public",
                      url: sharedFormSubmitUrl(
                        Number(artifactId),
                        token,
                        version != null && version !== "" ? Number(version) : null,
                      ),
                      // wire-form-entry-advanced：动态选项/回填走公开选项解析
                      // 端点（钉死版本；免登录绝不携带 Authorization）。
                      optionsUrl: sharedFormOptionsUrl(
                        Number(artifactId),
                        token,
                        version != null && version !== "" ? Number(version) : null,
                      ),
                    }}
                  />
                </div>
              </div>
            )}

            {state.kind === "ready" && mode === "doc" && (
              // 正文只读页（wire-rich-text-docs 任务 5.2）：复用控制台同一
              // 渲染器 MarkdownView（零 innerHTML——「渲染安全底线」对编辑
              // 预览与公开分享页同样适用）；无编辑入口、无导出入口。
              <div data-testid="share-doc">
                <h1
                  data-testid="share-doc-title"
                  className="text-lg font-semibold text-slate-900"
                >
                  {state.title}
                </h1>
                <div className="mt-4">
                  <MarkdownView source={docTextOf(state.spec)} />
                </div>
              </div>
            )}
          </>
        )}
      </main>

      {/* 页脚署名（wire-share-branding）：就绪态三段「{标题} · 由 寻数·柏讯
          提供 · 版本 v{n}」；非就绪态（loading/invalid/disabled）产品署名。
          record-view 以抬升 meta 裁决（found=false 等非就绪态产品署名）。 */}
      <footer
        data-testid="share-footer"
        className="border-t border-slate-200 bg-white py-4 text-center text-xs text-slate-400"
      >
        {state.kind === "ready"
          ? t("console.dashboard.share.footerVersion", {
              title: state.title,
              version: state.version,
            })
          : mode === "record-view" && recordMeta != null
            ? t("console.dashboard.share.footerVersion", {
                title: recordMeta.title,
                version: recordMeta.version,
              })
            : t("console.dashboard.share.footer")}
      </footer>
    </div>
  );
}
