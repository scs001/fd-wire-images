// 富文本文档分栏编辑器（wire-rich-text-docs 任务 4.1，design D1/D2/D3）。
//
// 形态：左 Markdown 源 textarea / 右 MarkdownView 实时预览（分栏；窄屏单列）。
// 正文事实源是 Markdown 源文本（D1）——预览只是渲染器输出，编辑器永不改源。
//
// 草稿层语义（D3，本组件的状态机核心）：
//   - 挂载：并行拉产物详情 + 草稿层；初始正文 = draft.text ?? 版本 spec.text
//     ?? ""（「优先草稿、回退版本」的判定只在这一处——服务端两个端点都不替
//     前端合并）。草稿层是「工作区」：一对一行、覆盖写、**绝不**进版本链。
//   - 输入停顿 debounce ~1200ms → PUT /draft 自动保存；保存状态如实四态
//     （未保存/保存中/已保存（时刻）/保存失败）；文本与「上次成功保存的草稿」
//     相同即不发请求（避免「保存 → 文本未变 → 再保存」的空转，也避免把
//     「刷新后内容本来就等于草稿」误标成未保存）。
//   - 卸载即取消挂起的 debounce（clearTimeout），不把定时器带进下一次挂载。
//   - 「保存版本」显式走既有 PUT 版本链（saveArtifactSpec，信封 {title,
//     spec:{format:"markdown", text}}）；成功后**不清草稿层**——草稿层是
//     工作区、版本链是里程碑，两者并存是预期语义（D3 硬约束）。
//
// 归档（archived）：textarea 只读、保存按钮禁用，如实提示「恢复后可继续编辑」
// （服务端 409 是兜底，UI 不给假入口）。
//
// 预览复用 doc/md.tsx 的 MarkdownView——零 innerHTML 的安全底线在该渲染器
// 一处成立，编辑器只负责把源文本递进去（D2：注入面收敛到唯一渲染器）。
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  apiErrorText,
  fetchDocDraft,
  fetchLifecycleArtifact,
  saveArtifactSpec,
  saveDocDraft,
  type Artifact,
  type ArtifactDetail,
} from "../api";
import { MarkdownView } from "./md";

/** 自动保存 debounce 毫秒数：停顿约 1.2s 落草稿层（高频击键不逐键发请求）。 */
const AUTOSAVE_DEBOUNCE_MS = 1200;

/** 保存状态四态（如实呈现，绝不用「已保存」掩盖在途/失败）。 */
type SaveStatus =
  | { kind: "idle" } // 未保存（当前文本 ≠ 上次成功保存的草稿）
  | { kind: "saving" } // 保存中…
  | { kind: "saved"; at: string | null } // 已保存（hh:mm）
  | { kind: "failed"; detail: string }; // 保存失败（重试）

/** 从版本 spec 信封里宽容取正文：非 doc 形状回退空串（绝不白屏）。 */
function specTextOf(spec: Record<string, unknown> | undefined): string {
  const inner = spec?.spec;
  if (inner && typeof inner === "object") {
    const text = (inner as { text?: unknown }).text;
    if (typeof text === "string") return text;
  }
  return "";
}

/** 保存时刻呈现：本地 hh:mm（后端返回 naive UTC，仅为「已保存（时刻）」提示，
 *  不做时区换算——用户关心的是「刚刚存过」）。 */
function savedAtLabel(updatedAt: string | null): string {
  const source = updatedAt ? new Date(updatedAt) : new Date();
  if (Number.isNaN(source.getTime())) return "";
  return `${String(source.getHours()).padStart(2, "0")}:${String(
    source.getMinutes(),
  ).padStart(2, "0")}`;
}

export function DocEditor({
  artifactId,
  onSaved,
  startEditing = false,
}: {
  artifactId: number;
  /** 保存版本成功回调（页面行版本号/标题同步——照 RecordViewPane 范式）。 */
  onSaved: (artifact: Artifact) => void;
  /** 刚空白创建的文档：挂载即聚焦正文（「创建即跳编辑器」——光标落在
   *  写作位；其余入口缺省不抢焦点）。 */
  startEditing?: boolean;
}) {
  const { t } = useTranslation();
  const [detail, setDetail] = useState<ArtifactDetail | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [title, setTitle] = useState("");
  const [text, setText] = useState("");
  /** 上次成功保存/加载到的草稿正文（自动保存的空转判定基准）。 */
  const [savedText, setSavedText] = useState<string | null>(null);
  const [status, setStatus] = useState<SaveStatus>({ kind: "idle" });
  /** 最近一次「保存版本」落进版本链的正文（呈现态的另一条持久化基线：
   *  正文在版本链里也算「已保存」——保存版本成功后状态转已保存而不必等
   *  草稿层追上；草稿层的自动保存照旧，绝不因版本保存而跳过）。 */
  const [versionText, setVersionText] = useState<string | null>(null);
  const [versionError, setVersionError] = useState<string | null>(null);
  const [savingVersion, setSavingVersion] = useState(false);

  // ── 挂载：详情 + 草稿层并行拉取（任务 4.1）────────────────────────────────
  useEffect(() => {
    let alive = true;
    setDetail(null);
    setLoadFailed(false);
    setStatus({ kind: "idle" });
    // 详情与草稿层并行（草稿层依赖产物存在，但两请求各自鉴权/404 语义独立；
    // 详情失败即整面失败——草稿层单独失败不阻塞打开，见下方 catch）。
    fetchLifecycleArtifact(artifactId)
      .then((row) => {
        if (!alive) return;
        setDetail(row);
        setTitle(row.title);
        const versionText = specTextOf(row.spec);
        // 初始正文 = 草稿层优先、无则当前版本 spec.text（D3 读路径）。
        return fetchDocDraft(artifactId)
          .then((draft) => {
            if (!alive) return;
            const initialText =
              typeof draft.text === "string" ? draft.text : versionText;
            setText(initialText);
            setSavedText(initialText);
            // 有草稿行且与版本不同：初始态就是「已保存」的草稿（不是未保存）。
            setStatus(
              draft.text != null
                ? { kind: "saved", at: savedAtLabel(draft.updated_at) }
                : { kind: "idle" },
            );
          })
          .catch(() => {
            // 草稿层读取失败（如端点未就绪）：回退版本正文并如实置「未保存」
            // （后续编辑仍会尝试自动保存）——绝不让一个可选层把整个编辑器拦下。
            if (!alive) return;
            setText(versionText);
            setSavedText(versionText);
          });
      })
      .catch(() => {
        if (alive) setLoadFailed(true);
      });
    return () => {
      alive = false;
    };
  }, [artifactId]);

  const locked = detail?.status === "archived";
  /** 呈现在途/失败判定的「有待落层正文」：正文不等于草稿层基线即草稿
   *  未落后（重试入口的显示条件——版本链基线不减少草稿层这一层的欠账）。 */
  const draftDirty = savedText != null && text !== savedText;
  /** 呈现用状态：saved 是「当时文本已持久化」的快照——正文必须命中两条
   *  持久化基线之一（草稿层或版本链），否则回落「未保存」：不让旧「已保存
   *  （时刻）」掩盖新的未落层正文。 */
  const persisted =
    savedText != null && text === savedText
      ? true
      : versionText != null && text === versionText;
  const shownStatus: SaveStatus =
    status.kind === "saved" && !persisted ? { kind: "idle" } : status;

  // ── 自动保存（debounce → PUT /draft；D3）──────────────────────────────────
  // 引用面：定时器/在途正文用 ref 收口，不把 text 写进 effect 依赖导致每次
  // 击键都重建定时器之外的多余重跑。
  const textRef = useRef(text);
  textRef.current = text;
  const lockedRef = useRef(locked);
  lockedRef.current = locked;
  /** 自动保存在途序号（迟到响应守卫——见 runAutosave）。 */
  const saveSeqRef = useRef(0);
  // 空白新建即聚焦正文（startEditing）：光标直接落在写作位。
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  useEffect(() => {
    if (startEditing) textareaRef.current?.focus();
  }, [startEditing]);

  const runAutosave = useCallback(async () => {
    const pending = textRef.current;
    if (lockedRef.current) return; // 归档只读：不发写请求（服务端 409 是兜底）
    // 序号守卫：连发保存（重试/继续击键）时只认最后一次的响应——旧响应
    // 迟到不得把「已保存基线」回退成旧正文（否则会误标未保存并二次空转）。
    const seq = ++saveSeqRef.current;
    setStatus({ kind: "saving" });
    try {
      const draft = await saveDocDraft(artifactId, pending);
      if (seq !== saveSeqRef.current) return;
      setSavedText(pending);
      setStatus({ kind: "saved", at: savedAtLabel(draft.updated_at) });
    } catch (err) {
      if (seq !== saveSeqRef.current) return;
      // 超限 422 / 归档 409 / 网络错：后端 detail 如实呈现（无 detail 兜底）。
      setStatus({
        kind: "failed",
        detail: apiErrorText(err, t("console.doc.editor.saveFailed", "保存失败，请稍后重试。")),
      });
    }
  }, [artifactId, t]);

  useEffect(() => {
    if (locked || savedText == null || text === savedText) return;
    const timer = window.setTimeout(() => {
      void runAutosave();
    }, AUTOSAVE_DEBOUNCE_MS);
    // 卸载/换产物/继续击键即取消挂起的保存——绝不把旧定时器带过挂载边界。
    return () => window.clearTimeout(timer);
  }, [text, savedText, locked, runAutosave]);

  // 失败态可重试（点状态行）：重发当前正文（不依赖 debounce 再触发）。
  const retryAutosave = () => {
    if (locked || text === savedText) return;
    void runAutosave();
  };

  /** 保存版本：显式里程碑（PUT 版本链追加；草稿层保留——D3 并存语义）。
   *  空标题不发请求：按钮禁用（照 RecordViewEditor 的 disabled 范式——
   *  「零请求拦截」走不可点，而不是点完再报错）。 */
  const saveVersion = async () => {
    if (savingVersion || locked) return;
    const nextTitle = title.trim();
    if (!nextTitle) return;
    setSavingVersion(true);
    setVersionError(null);
    try {
      const next = await saveArtifactSpec(artifactId, {
        title: nextTitle,
        spec: { format: "markdown", text },
      });
      setDetail(next);
      setTitle(next.title);
      // 正文已进版本链：状态即「已保存」（不必等草稿层的自动保存追上——
      // 版本链同样是持久化落点）；草稿层照旧由 debounce 落，绝不清也不跳。
      setVersionText(text);
      setStatus({ kind: "saved", at: savedAtLabel(null) });
      onSaved(next);
    } catch (err) {
      // 409（归档）/422（超限、格式）：后端 detail 如实呈现。
      setVersionError(
        apiErrorText(err, t("console.doc.editor.saveFailed", "保存失败，请稍后重试。")),
      );
    } finally {
      setSavingVersion(false);
    }
  };

  const statusText = useMemo(() => {
    if (shownStatus.kind === "saving")
      return t("console.doc.editor.saving", "保存中…");
    if (shownStatus.kind === "saved") {
      return t("console.doc.editor.saved", "已保存");
    }
    if (shownStatus.kind === "failed") {
      return t("console.doc.editor.saveFailed", "保存失败，请重试");
    }
    return t("console.doc.editor.unsaved", "未保存");
  }, [shownStatus, t]);
  /** 「已保存（HH:MM）」的时刻段单独渲染：i18n 键为纯文案（跨语言不卷时刻），
   *  时刻是机组事实——两段拼接避免把时间格式塞进翻译模板。 */
  const statusAt = shownStatus.kind === "saved" ? shownStatus.at : null;

  if (loadFailed) {
    return (
      <div
        data-testid="doc-editor"
        className="mt-3 rounded-md border border-red-200 bg-red-50/60 p-4"
      >
        <p data-testid="doc-editor-load-error" className="text-xs text-red-700">
          {t("console.doc.editor.loadError", "文档加载失败。")}
        </p>
      </div>
    );
  }

  if (detail == null) {
    return (
      <div
        data-testid="doc-editor"
        className="mt-3 rounded-md border border-slate-200 bg-slate-50/60 p-4"
      >
        <p data-testid="doc-editor-loading" className="text-xs text-slate-400">
          {t("console.doc.editor.loading", "文档加载中…")}
        </p>
      </div>
    );
  }

  return (
    <div
      data-testid="doc-editor"
      className="mt-3 rounded-md border border-slate-200 bg-slate-50/60 p-4"
    >
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold">
          {t("console.doc.editor.paneTitle", "富文本文档")}
        </h2>
        {locked && (
          <span className="rounded bg-slate-200 px-1.5 py-0.5 text-[10px] text-slate-600">
            {t("console.doc.editor.lockedBadge", "已归档 · 只读")}
          </span>
        )}
        <label htmlFor="doc-editor-title" className="ml-2 text-xs text-slate-500">
          {t("console.doc.editor.titleLabel", "文档标题")}
        </label>
        <input
          id="doc-editor-title"
          data-testid="doc-editor-title"
          value={title}
          disabled={locked}
          onChange={(e) => {
            setTitle(e.target.value);
            setVersionError(null);
          }}
          className="w-56 rounded-md border border-slate-300 px-2 py-1 text-sm disabled:opacity-50"
        />
        <div className="ml-auto flex items-center gap-2">
          {/* 保存状态四态如实呈现（未保存/保存中/已保存（时刻）/保存失败重试）。 */}
          <span
            data-testid="doc-editor-status"
            data-status={shownStatus.kind}
            className={
              shownStatus.kind === "failed"
                ? "text-[11px] text-red-600"
                : shownStatus.kind === "saved"
                  ? "text-[11px] text-emerald-600"
                  : "text-[11px] text-slate-400"
            }
          >
            {statusText}
            {statusAt ? `（${statusAt}）` : ""}
          </span>
          {shownStatus.kind === "failed" && !locked && draftDirty && (
            <button
              type="button"
              data-testid="doc-editor-retry"
              onClick={retryAutosave}
              className="rounded border border-slate-300 bg-white px-2 py-1 text-xs text-slate-600 hover:bg-slate-100"
            >
              {t("console.doc.editor.retry", "重试")}
            </button>
          )}
          <button
            type="button"
            data-testid="doc-editor-save"
            disabled={locked || savingVersion || !title.trim()}
            onClick={() => void saveVersion()}
            className="rounded bg-slate-900 px-3 py-1 text-xs text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {savingVersion
              ? t("console.doc.editor.saving", "保存中…")
              : t("console.doc.editor.saveVersion", "保存版本")}
          </button>
        </div>
      </div>

      {locked && (
        <p
          data-testid="doc-editor-archived-notice"
          className="mt-1 text-[11px] text-amber-700"
        >
          {/* key=console.doc.editor.archivedNotice（i18n 键在位时以其为准）。 */}
          {t("console.doc.editor.archivedNotice", "该文档已归档（只读）。")}
        </p>
      )}
      {versionError && (
        <p data-testid="doc-editor-version-error" className="mt-1 text-[11px] text-red-600">
          {versionError}
        </p>
      )}
      {shownStatus.kind === "failed" && shownStatus.detail && (
        <p data-testid="doc-editor-status-error" className="mt-1 text-[11px] text-red-600">
          {shownStatus.detail}
        </p>
      )}

      {/* 分栏：左 Markdown 源 / 右实时预览（窄屏单列纵向）。 */}
      <div className="mt-3 grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div className="flex flex-col">
          <span className="text-[11px] text-slate-400">
            {t("console.doc.editor.sourceLabel", "Markdown 源")}
          </span>
          <textarea
            ref={textareaRef}
            data-testid="doc-editor-text"
            value={text}
            readOnly={locked}
            disabled={locked}
            onChange={(e) => setText(e.target.value)}
            placeholder={t(
              "console.doc.editor.textPlaceholder",
              "在此书写 Markdown 正文（# 标题、- 列表、**强调**、[链接](https://…)、![图](https://…)）…",
            )}
            spellCheck={false}
            rows={16}
            className="mt-1 min-h-[24rem] flex-1 w-full resize-y rounded-md border border-slate-300 bg-white p-3 font-mono text-xs text-slate-800 focus:outline-none focus:ring-2 focus:ring-slate-400 disabled:bg-slate-50"
          />
        </div>
        <div className="flex flex-col">
          <span className="text-[11px] text-slate-400">
            {t("console.doc.editor.previewLabel", "实时预览")}
          </span>
          <div
            data-testid="doc-editor-preview"
            className="mt-1 min-h-[24rem] flex-1 overflow-auto rounded-md border border-slate-200 bg-white p-3"
          >
            {/* 零 innerHTML 的渲染器（D2）：源文本原样递入，输出 React 节点。 */}
            <MarkdownView source={text} />
          </div>
        </div>
      </div>
    </div>
  );
}
