import { useEffect, useRef, useState } from 'react';
import { BookOpen, Copy, Download, RefreshCw, X } from 'lucide-react';
import { attachmentUrl } from './api';
import { getOriginalModelRequest, getRequestTrace, type ModelCallTrace, type RequestTrace, type TraceTarget } from './trace-api';

const statuses: Record<string, string> = {
  running: '进行中', completed: '已完成', failed: '失败', cancelled: '已停止', interrupted: '已中断',
};
const roles: Record<string, string> = { system: '系统', user: '用户', assistant: '助手', tool: '工具结果', developer: '开发者' };
const pretty = (value: unknown) => JSON.stringify(value, null, 2);
const date = (value: string) => new Date(value).toLocaleString('zh-CN', { hour12: false });
const elapsed = (ms: number | null) => ms === null ? '' : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(2)} s`;

/** 保持内容块原顺序，文字完整展示；图片引用用附件接口预览。 */
function MessageContent({ content, call }: { content: unknown; call: ModelCallTrace }) {
  if (typeof content === 'string') return <pre className="trace-text">{content || '（空正文）'}</pre>;
  if (!Array.isArray(content)) return <pre className="trace-text">{pretty(content)}</pre>;
  return <div className="trace-blocks">{content.map((block: Record<string, unknown>, index) => {
    if (block.type === 'text') return <pre className="trace-text" key={index}>{String(block.text ?? '')}</pre>;
    if (block.type === 'image_url') {
      const url = typeof block.image_url === 'object' && block.image_url !== null
        ? String((block.image_url as Record<string, unknown>).url ?? '') : '';
      const image = call.images.find((item) => url === `attachment://${item.attachment.id}`);
      return image ? <figure className="trace-image" key={index}>
        <a href={attachmentUrl(image.attachment)} target="_blank" rel="noreferrer">
          <img src={attachmentUrl(image.attachment)} alt={image.attachment.name} loading="lazy" /></a>
        <figcaption>{image.attachment.name}</figcaption>
      </figure> : <pre className="trace-text" key={index}>{pretty(block)}</pre>;
    }
    return <pre className="trace-text" key={index}>{pretty(block)}</pre>;
  })}</div>;
}

/** 原生 dialog 负责焦点约束和 Escape，关闭后由聊天页恢复入口焦点。 */
export default function TraceDrawer({ target, onClose }: { target: TraceTarget; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const operation = useRef<AbortController | null>(null);
  const [data, setData] = useState<RequestTrace | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState('');

  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    return () => { element?.close(); operation.current?.abort(); };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(''); setNotice('');
    // 延后到下一任务，StrictMode 的即时清理可撤销尚未发出的请求。
    const timer = window.setTimeout(() => {
      getRequestTrace(target, controller.signal).then((value) => {
        if (!controller.signal.aborted) setData(value);
      }).catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : '调用记录读取失败');
      }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    }, 0);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [target.conversationId, target.requestId, revision]);

  /** 完整导出从服务端还原图片，失败时不给用户一份伪完整请求。 */
  async function exportRequest(call: ModelCallTrace, download: boolean) {
    if (operation.current) return;
    const controller = new AbortController();
    operation.current = controller;
    setBusy(call.id); setNotice('');
    try {
      const body = await getOriginalModelRequest(target, call.id, controller.signal);
      if (controller.signal.aborted) return;
      const text = pretty(body);
      if (download) {
        const url = URL.createObjectURL(new Blob([text], { type: 'application/json;charset=utf-8' }));
        const link = document.createElement('a');
        link.href = url; link.download = `model-request-${call.id}.json`;
        link.click(); window.setTimeout(() => URL.revokeObjectURL(url), 1000);
        setNotice('完整请求已下载');
      } else {
        await navigator.clipboard.writeText(text);
        if (!controller.signal.aborted) setNotice('完整请求已复制');
      }
    } catch (reason: unknown) {
      if (!controller.signal.aborted) setNotice(reason instanceof Error ? reason.message : '导出失败，可尝试下载');
    } finally {
      if (!controller.signal.aborted) setBusy(null);
      if (operation.current === controller) operation.current = null;
    }
  }

  return <dialog ref={dialog} className="trace-drawer" aria-labelledby="trace-title"
    onCancel={(event) => { event.preventDefault(); onClose(); }}
    onClick={(event) => { if (event.target === event.currentTarget) {
      const bounds = event.currentTarget.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) onClose();
    } }}>
    <header className="trace-header">
      <div><h2 id="trace-title">调用详情</h2><p>本次提问实际使用的资料和模型请求</p></div>
      <button className="icon-button" autoFocus title="关闭" aria-label="关闭调用详情" onClick={onClose}><X size={19} /></button>
    </header>
    <div className="trace-toolbar">
      <span title={target.requestId}>请求 <code>{target.requestId.slice(0, 8)}</code></span>
      <button className="text-button" disabled={loading} onClick={() => setRevision((value) => value + 1)}>
        <RefreshCw size={13} className={loading ? 'trace-spinning' : ''} />刷新
      </button>
    </div>
    {notice && <div className="trace-notice" role="status">{notice}</div>}
    <div className="trace-scroll" aria-busy={loading}>
      {loading && !data && <p className="trace-empty" role="status">正在读取调用记录…</p>}
      {error && <div className="trace-empty" role="alert"><p>{error}</p><button className="text-button" onClick={() => setRevision((value) => value + 1)}>重试</button></div>}
      {!loading && !error && !data?.attempts.length && <p className="trace-empty">这次提问暂无调用记录。旧消息不会补造记录，生成刚开始时可稍后刷新。</p>}
      {!error && data?.attempts.map((attempt, attemptIndex) => <section className="trace-attempt" key={attempt.id}>
        <div className="trace-attempt-heading"><strong>执行 {attemptIndex + 1}</strong><span className={`trace-status status-${attempt.status}`}>{statuses[attempt.status] ?? attempt.status}</span></div>
        <p className="trace-meta">{date(attempt.created_at)} · {attempt.model}</p>
        <details className="trace-identifiers"><summary>关联编号</summary><p>执行：{attempt.id}</p><p>日志请求：{attempt.http_request_id}</p><p>消息请求：{target.requestId}</p></details>
        {attempt.knowledge_enabled ? <details className="trace-card" open>
          <summary><BookOpen size={14} /><strong>知识库检索</strong><span>{attempt.retrieval ? elapsed(attempt.retrieval.elapsed_ms) : '尚无结果'}</span></summary>
          <div className="trace-card-body">
            {attempt.retrieval ? <>
              <p className="trace-label">实际查询</p><pre className="trace-text">{attempt.retrieval.query}</pre>
              {attempt.retrieval.error && <p className="trace-error">{attempt.retrieval.error}</p>}
              <p className="trace-meta">{attempt.retrieval.citations.length ? `以下 ${attempt.retrieval.citations.length} 个片段为检索最终结果，实际发送内容见下方模型请求` : attempt.retrieval.message}</p>
              {attempt.retrieval.citations.map((citation) => <details className="trace-source" key={citation.chunk_id}>
                <summary><span>[{citation.number}] {citation.title}</span><small>{citation.location}</small></summary>
                <pre className="trace-text">{citation.quote}</pre><p className="trace-id">版本：{citation.version_id}<br />片段：{citation.chunk_id}</p>
              </details>)}
            </> : <p className="trace-meta">检索尚未完成或记录未保存，可刷新查看。</p>}
          </div>
        </details> : <p className="trace-meta trace-knowledge-off">知识库未开启</p>}
        {attempt.calls.map((call) => {
          const body = call.request_body;
          const messages = body && Array.isArray(body.messages) ? body.messages as Record<string, unknown>[] : [];
          const params = body ? Object.fromEntries(Object.entries(body).filter(([key]) => !['messages', 'tools'].includes(key))) : null;
          return <details className="trace-card trace-call" open key={call.id}>
            <summary><strong>模型调用 {call.sequence}</strong><span className={`trace-status status-${call.status}`}>{statuses[call.status] ?? call.status}</span><span>{elapsed(call.elapsed_ms)}</span></summary>
            <div className="trace-card-body">
              {call.error && <p className="trace-error">{call.error}{call.http_status ? ` · HTTP ${call.http_status}` : ''}</p>}
              {call.capture_error && <p className="trace-error">{call.capture_error}</p>}
              {!body ? <p className="trace-meta">尚未捕获到请求参数</p> : <>
                <p className="trace-label">发送的消息 · {messages.length} 条</p>
                {messages.map((message, index) => <details className="trace-message" key={index}>
                  <summary><span className="trace-message-number">{index + 1}</span><strong>{roles[String(message.role)] ?? String(message.role)}</strong>
                    <span>{typeof message.content === 'string' ? `${message.content.length} 字符` : '内容块'}</span></summary>
                  <MessageContent content={message.content} call={call} />
                  {Object.keys(message).some((key) => !['role', 'content'].includes(key)) && <pre className="trace-json">{pretty(Object.fromEntries(Object.entries(message).filter(([key]) => !['role', 'content'].includes(key))))}</pre>}
                </details>)}
                {body.tools !== undefined && <details className="trace-message"><summary>工具定义</summary><pre className="trace-json">{pretty(body.tools)}</pre></details>}
                <details className="trace-message"><summary>请求参数</summary><pre className="trace-json">{pretty(params)}</pre></details>
                <details className="trace-message"><summary>请求 JSON{call.images.length > 0 ? '（图片为附件引用）' : ''}</summary><pre className="trace-json">{pretty(body)}</pre></details>
                <div className="trace-export">
                  <button className="text-button" disabled={!!busy || !!call.capture_error} onClick={() => void exportRequest(call, false)}><Copy size={13} />复制完整请求</button>
                  <button className="text-button" disabled={!!busy || !!call.capture_error} onClick={() => void exportRequest(call, true)}><Download size={13} />下载 JSON</button>
                  {busy === call.id && <span className="small-spinner" />}
                </div>
                {call.images.length > 0 && <p className="trace-meta">复制和下载会恢复图片 Base64，内容可能较大。</p>}
              </>}
            </div>
          </details>;
        })}
        {!attempt.calls.length && <p className="trace-meta">尚无模型调用记录</p>}
      </section>)}
    </div>
  </dialog>;
}
