// 最小 Markdown 渲染器（wire-rich-text-docs 任务 3.1-3.3，design D2）。
//
// 安全底线是**结构性的**：本模块从不产生 innerHTML（本文件全篇不含
// dangerouslySetInnerHTML），输出一律是 React 元素——React 文本子节点自动
// 转义，于是「正文里写 <script>/onerror= 会不会执行」这类问题在结构上就没有
// 可执行的答案。这正是选择「零依赖自研最小渲染器」而非 marked+DOMPurify 的
// 关键理由：消毒链是「事后补锁」，且消毒配置漂移本身就是常见事故源；零
// innerHTML 把锁放进结构里（design D2 决策原文）。
//
// 子集外语法（表格/脚注/内嵌 HTML 标签）不做对应结构——按纯文本如实呈现，
// 不静默吞字符（任务 3.3）。将来若要完整 Markdown，替换本模块即可（渲染入口
// 不变），但「不产生 innerHTML」必须保持。
//
// 属性面的两道闸（任务 3.2）：
//   - 链接只允许 http/https/mailto；其余（javascript:、data: 等）不渲染为
//     可点击元素，仅按纯文本呈现——文本是安全的落点。
//   - 图片源只允许 http/https；其余按 alt 纯文本呈现，不产生 <img>。

import type { ReactNode } from "react";

// ---------------------------------------------------------------------------
// URL 策略
// ---------------------------------------------------------------------------

/** 链接允许的协议（相对路径也是安全的——浏览器同源策略覆盖不到 javascript: 之类）。 */
function safeLinkHref(raw: string): string | null {
  const url = raw.trim();
  if (!url) return null;
  // 相对路径 / 锚点 / 站内路径：无协议前缀，安全。
  if (/^[/#?]/.test(url)) return url;
  if (/^https?:\/\//i.test(url) || /^mailto:/i.test(url)) return url;
  // 其余一律拒绝（javascript:、data:、vbscript:…）
  return null;
}

/** 图片源只认 http(s)：外链图片是 v1 的唯一插图形态（design D6）。 */
function safeImageSrc(raw: string): string | null {
  const url = raw.trim();
  return /^https?:\/\//i.test(url) ? url : null;
}

// ---------------------------------------------------------------------------
// 行内解析（输出 React 节点；文本子节点由 React 自动转义）
// ---------------------------------------------------------------------------

// 行内元素按「先长后短」的优先级匹配：代码 span 最先生效（其内容字面量），
// 随后图片、链接，最后粗体、斜体。顺序错了会出现「链接里再解析出粗体」之类
// 的嵌套惊喜——v1 不追求完整 CommonMark，只求行为可预期。
//
// 下划线强调按 CommonMark 词边界纪律（任务 7.2 浏览器实测抓出）：
// `window.__XSS_EXECUTED` 这类 snake_case/dunder 标识符**不得**被解析成
// 斜体——`_` 强调只允许出现在词边界（前后非 [\w]）；星号强调不设此限
// （CommonMark 允许 intraword `*`）。`__强__` 同 `**强**` 走 strong。
const INLINE_PATTERN =
  /(`[^`\n]+`)|(!\[([^\]]*)\]\(([^)\s]+)\))|(\[([^\]]+)\]\(([^)\s]+)\))|(\*\*[^*\n]+\*\*)|((?<![\w])__[^_\n]+__(?![\w]))|(\*[^*\n]+\*)|((?<![\w])_[^_\n]+_(?![\w]))/;

function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const out: ReactNode[] = [];
  let rest = text;
  let index = 0;

  while (rest.length > 0) {
    const match = INLINE_PATTERN.exec(rest);
    if (match === null || match.index > 0) {
      const plain = match === null ? rest : rest.slice(0, match.index);
      if (plain) out.push(plain);
      if (match === null) break;
      rest = rest.slice(match.index);
      continue;
    }

    const [token] = match;
    const key = `${keyPrefix}-${index}`;
    index += 1;

    if (token.startsWith("`")) {
      // 行内代码：内容字面量，不再解析
      out.push(<code key={key}>{token.slice(1, -1)}</code>);
    } else if (token.startsWith("![")) {
      const alt = match[3] ?? "";
      const src = match[4] ?? "";
      const safe = safeImageSrc(src);
      if (safe !== null) {
        out.push(<img key={key} src={safe} alt={alt} className="max-w-full rounded" />);
      } else {
        // 非法源按 alt 纯文本呈现（不产生 <img>，任务 3.2）
        out.push(alt);
      }
    } else if (token.startsWith("[")) {
      const label = match[6] ?? "";
      const href = safeLinkHref(match[7] ?? "");
      if (href !== null) {
        out.push(
          <a key={key} href={href} target="_blank" rel="noopener noreferrer">
            {renderInline(label, `${key}-label`)}
          </a>,
        );
      } else {
        // 非法协议不渲染为可点击元素（javascript: 等），仅文本
        out.push(label);
      }
    } else if (token.startsWith("**")) {
      out.push(<strong key={key}>{token.slice(2, -2)}</strong>);
    } else if (token.startsWith("__")) {
      out.push(<strong key={key}>{token.slice(2, -2)}</strong>);
    } else {
      out.push(<em key={key}>{token.slice(1, -1)}</em>);
    }

    rest = rest.slice(token.length);
  }

  return out;
}

// ---------------------------------------------------------------------------
// 块级解析
// ---------------------------------------------------------------------------

type Block =
  | { type: "heading"; level: number; text: string }
  | { type: "paragraph"; lines: string[] }
  | { type: "code"; lines: string[] }
  | { type: "quote"; lines: string[] }
  | { type: "ul"; items: string[] }
  | { type: "ol"; items: string[] };

function parseBlocks(source: string): Block[] {
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  const blocks: Block[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    // 空行：分块边界
    if (line.trim() === "") {
      i += 1;
      continue;
    }

    // 围栏代码块：内容原样（不再做行内解析）
    if (/^\s*```/.test(line)) {
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !/^\s*```/.test(lines[i])) {
        body.push(lines[i]);
        i += 1;
      }
      i += 1; // 跳过收尾围栏（未闭合则到文件尾，容错）
      blocks.push({ type: "code", lines: body });
      continue;
    }

    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      blocks.push({ type: "heading", level: heading[1].length, text: heading[2].trim() });
      i += 1;
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      const body: string[] = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        body.push(lines[i].replace(/^\s*>\s?/, ""));
        i += 1;
      }
      blocks.push({ type: "quote", lines: body });
      continue;
    }

    if (/^\s*[-*]\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*]\s+/, ""));
        i += 1;
      }
      blocks.push({ type: "ul", items });
      continue;
    }

    if (/^\s*\d+\.\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*\d+\.\s+/, ""));
        i += 1;
      }
      blocks.push({ type: "ol", items });
      continue;
    }

    // 其余：连续非空行组成段落（软换行在渲染时保留为空格）
    const lines2: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() !== "" &&
      !/^\s*```/.test(lines[i]) &&
      !/^(#{1,6})\s+/.test(lines[i]) &&
      !/^\s*>\s?/.test(lines[i]) &&
      !/^\s*[-*]\s+/.test(lines[i]) &&
      !/^\s*\d+\.\s+/.test(lines[i])
    ) {
      lines2.push(lines[i]);
      i += 1;
    }
    blocks.push({ type: "paragraph", lines: lines2 });
  }

  return blocks;
}

// ---------------------------------------------------------------------------
// 渲染入口
// ---------------------------------------------------------------------------

const HEADING_CLASS: Record<number, string> = {
  1: "text-2xl font-semibold",
  2: "text-xl font-semibold",
  3: "text-lg font-semibold",
  4: "text-base font-semibold",
  5: "text-sm font-semibold",
  6: "text-sm font-semibold text-slate-600",
};

export function MarkdownView({ source }: { source: string }) {
  const blocks = parseBlocks(source);

  return (
    <div className="space-y-3 text-sm leading-6 text-slate-800" data-testid="doc-markdown">
      {blocks.map((block, bi) => {
        const key = `b-${bi}`;
        if (block.type === "heading") {
          const level = Math.min(Math.max(block.level, 1), 6);
          const Tag = `h${level}` as "h1" | "h2" | "h3" | "h4" | "h5" | "h6";
          return (
            <Tag key={key} className={HEADING_CLASS[level]}>
              {renderInline(block.text, key)}
            </Tag>
          );
        }
        if (block.type === "code") {
          return (
            <pre key={key} className="overflow-x-auto rounded bg-slate-900 p-3 text-xs text-slate-100">
              <code>{block.lines.join("\n")}</code>
            </pre>
          );
        }
        if (block.type === "quote") {
          return (
            <blockquote key={key} className="border-l-4 border-slate-300 pl-3 text-slate-600">
              {block.lines.map((line, li) => (
                <p key={`${key}-${li}`}>{renderInline(line, `${key}-${li}`)}</p>
              ))}
            </blockquote>
          );
        }
        if (block.type === "ul") {
          return (
            <ul key={key} className="list-disc space-y-1 pl-5">
              {block.items.map((item, li) => (
                <li key={`${key}-${li}`}>{renderInline(item, `${key}-${li}`)}</li>
              ))}
            </ul>
          );
        }
        if (block.type === "ol") {
          return (
            <ol key={key} className="list-decimal space-y-1 pl-5">
              {block.items.map((item, li) => (
                <li key={`${key}-${li}`}>{renderInline(item, `${key}-${li}`)}</li>
              ))}
            </ol>
          );
        }
        return (
          <p key={key}>{renderInline(block.lines.join(" "), key)}</p>
        );
      })}
    </div>
  );
}
