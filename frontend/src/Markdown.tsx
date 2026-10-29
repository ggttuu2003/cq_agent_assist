import { useRef, useState, type ReactNode } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { Check, Copy } from 'lucide-react';

/** 为代码块增加独立复制按钮；复制的是纯代码，不包含按钮文字。 */
function CodeBlock({ children }: { children: ReactNode }) {
  const codeRef = useRef<HTMLPreElement>(null);
  const [copied, setCopied] = useState(false);
  const [failed, setFailed] = useState(false);

  /** 复制成功后短暂显示确认状态，失败时提示用户手动选择代码。 */
  async function copyCode() {
    try {
      await navigator.clipboard.writeText(codeRef.current?.textContent ?? '');
      setCopied(true);
      setFailed(false);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      setFailed(true);
    }
  }

  return (
    <div className="code-block">
      <div className="code-toolbar">
        <span>代码</span>
        <button onClick={copyCode} aria-label={copied ? '代码已复制' : '复制代码'}>
          {copied ? <Check size={13} /> : <Copy size={13} />}
          {copied ? '已复制' : failed ? '请手动选择复制' : '复制'}
        </button>
      </div>
      <pre ref={codeRef}>{children}</pre>
    </div>
  );
}

/** 展示模型返回的 Markdown；不启用原始 HTML，避免把回答作为网页脚本执行。 */
export default function Markdown({ content }: { content: string }) {
  return (
    <div className="markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={{
          pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,
          a: ({ children, href }) => (
            <a href={href} target="_blank" rel="noopener noreferrer">
              {children}
            </a>
          ),
          table: ({ children }) => (
            <div className="table-scroll">
              <table>{children}</table>
            </div>
          ),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
