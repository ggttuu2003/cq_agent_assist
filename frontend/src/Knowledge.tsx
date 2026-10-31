import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { ArrowLeft, BookOpen, Check, ChevronRight, Download, FileText, PanelLeftOpen, RefreshCw, Search, Trash2, Upload, X } from 'lucide-react';
import type { Citation } from './api';
import { deleteKnowledgeDocument, knowledgeChunks, knowledgeContent, knowledgeDocument, knowledgeDocuments, knowledgeSummary,
  renameKnowledgeDocument, retryKnowledgeDocument, searchKnowledge, uploadKnowledgeDocument,
  type ChunkPage, type ContentPage, type KnowledgeDocument, type KnowledgePage, type KnowledgeSummary, type KnowledgeTarget } from './knowledge-api';

const statuses: Record<string, string> = { queued: '待处理', parsing: '解析中', indexing: '建立索引中', ready: '可检索',
  failed: '处理失败', update_failed: '更新失败', deleting: '删除中', cleanup_failed: '清理失败' };
const processing = (item: KnowledgeDocument) => ['queued', 'parsing', 'indexing', 'deleting'].includes(item.status);
const errorText = (error: unknown) => error instanceof Error ? error.message : '操作失败，请重试';
const fileSize = (size: number) => size < 1024 ? `${size} B` : size < 1024 * 1024 ? `${(size / 1024).toFixed(1)} KB` : `${(size / 1024 / 1024).toFixed(1)} MB`;

/** 知识库只负责资料管理，上传后自动入库，聊天中由 Agent 自行检索。 */
export default function Knowledge({ target, onBack, onMenu, showMenu }: {
  target: KnowledgeTarget | null; onBack: () => void; onMenu: () => void; showMenu: boolean;
}) {
  const [summary, setSummary] = useState<KnowledgeSummary | null>(null);
  const [page, setPage] = useState<KnowledgePage>({ items: [], next_cursor: null });
  const [query, setQuery] = useState('');
  const [error, setError] = useState('');
  const [listError, setListError] = useState('');
  const [notice, setNotice] = useState('');
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState<string[]>([]);
  const [selected, setSelected] = useState<KnowledgeTarget | null>(target);
  const [detail, setDetail] = useState<KnowledgeDocument | null>(null);
  const [content, setContent] = useState<ContentPage | null>(null);
  const [chunks, setChunks] = useState<ChunkPage | null>(null);
  const [detailError, setDetailError] = useState('');
  const [detailLoading, setDetailLoading] = useState(false);
  const [editingName, setEditingName] = useState('');
  const [searchResults, setSearchResults] = useState<Citation[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [reload, setReload] = useState(0);
  const [previewReload, setPreviewReload] = useState(0);
  const dialogRef = useRef<HTMLDialogElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const replacementRef = useRef<HTMLInputElement>(null);
  const uploadRef = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const listRequest = useRef<AbortController | null>(null);
  const searchRequest = useRef<AbortController | null>(null);
  const detailRequest = useRef<AbortController | null>(null);
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  const selectedRow = page.items.find((item) => item.id === selected?.documentId);

  useEffect(() => { setSelected(target); }, [target]);
  useEffect(() => {
    if (selected && !dialogRef.current?.open) dialogRef.current?.showModal();
  }, [selected]);
  useEffect(() => {
    // 只在新版本真正发布后更新已打开的预览，首屏列表到达不重复发送详情请求。
    if (detail && selectedRow?.status === 'ready' && selectedRow.active_version_id !== detail.active_version_id) {
      setDetail(null);
      setPreviewReload((value) => value + 1);
    }
  }, [selectedRow, detail]);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; uploadRef.current?.abort(); searchRequest.current?.abort();
      listRequest.current?.abort(); detailRequest.current?.abort(); };
  }, []);

  const load = useCallback(async (signal: AbortSignal, cursor?: string | null) => {
    const [information, documents] = await Promise.all([knowledgeSummary(signal), knowledgeDocuments(query.trim(), cursor, signal)]);
    if (!signal.aborted) {
      setSummary(information);
      setPage((previous) => cursor ? { ...documents, items: [...previous.items, ...documents.items] } : documents);
      setListError('');
    }
  }, [query]);

  useEffect(() => {
    const controller = new AbortController(); listRequest.current?.abort(); listRequest.current = controller;
    setLoading(true);
    // 延迟启动避免 StrictMode 的首次检查产生重复请求，搜索框同时进行防抖。
    const timer = window.setTimeout(() => { void load(controller.signal).catch((error) => {
      if (!controller.signal.aborted) setListError(errorText(error));
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); }); }, query ? 250 : 0);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [load, reload, query]);

  useEffect(() => {
    if (!summary?.processing && !page.items.some(processing)) return;
    const controller = new AbortController();
    let checking = false;
    const timer = window.setInterval(() => {
      if (checking || document.hidden || uploadRef.current) return;
      checking = true;
      // 处理进度只刷新当前首屏；避免把过期分页结果与新状态拼接。
      void load(controller.signal).catch(() => {}).finally(() => { checking = false; });
    }, 2500);
    return () => { window.clearInterval(timer); controller.abort(); };
  }, [load, summary?.processing, page.items.some(processing)]);

  useEffect(() => {
    detailRequest.current?.abort();
    if (!selected) { setDetail(null); setContent(null); setChunks(null); return; }
    const controller = new AbortController(); detailRequest.current = controller;
    setDetailLoading(true); setDetailError(''); setContent(null); setChunks(null); setDetail(null);
    const timer = window.setTimeout(async () => {
      try {
        const item = await knowledgeDocument(selected.documentId, controller.signal);
        if (controller.signal.aborted) return;
        setDetail(item); setEditingName(item.name);
        const [text, fragments] = await Promise.all([knowledgeContent(selected, 0, controller.signal), knowledgeChunks(selected, 0, controller.signal)]);
        if (!controller.signal.aborted) { setContent(text); setChunks(fragments); }
      } catch (error) { if (!controller.signal.aborted) setDetailError(errorText(error)); }
      finally { if (!controller.signal.aborted) setDetailLoading(false); }
    }, 0);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [selected, reload, previewReload]);

  useEffect(() => {
    if (!selected?.chunkId || !chunks) return;
    document.getElementById(`knowledge-chunk-${selected.chunkId}`)?.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }, [chunks, selected]);

  async function upload(files: File[], replaceId?: string) {
    if (!files.length || uploadRef.current) return;
    setNotice(''); setError('');
    const maxBatch = summary?.max_batch_files ?? 10;
    if (files.length > maxBatch) { setError(`每次最多上传 ${maxBatch} 个文件`); return; }
    if (files.some((file) => file.size > (summary?.max_file_size ?? 20 * 1024 * 1024))) { setError('单个文件不能超过 20 MB'); return; }
    const controller = new AbortController(); uploadRef.current = controller;
    setUploading(files.map((file) => file.name));
    const failures: string[] = [];
    try {
      // 顺序上传避免大批文件同时占满请求连接，每个文件在后端独立排队。
      for (const file of files) {
        try { await uploadKnowledgeDocument(file, controller.signal, replaceId); }
        catch (error) { if (controller.signal.aborted) return; failures.push(`${file.name}：${errorText(error)}`); }
        if (!controller.signal.aborted) setUploading((items) => items.slice(1));
      }
      if (mounted.current) {
        setError(failures.join('；'));
        setNotice(failures.length === files.length ? '' : failures.length ? '其余文件已提交处理' : '已提交，处理完成后可在聊天中开启知识库使用');
        setReload((value) => value + 1);
      }
    } finally {
      if (uploadRef.current === controller) { uploadRef.current = null; if (mounted.current) setUploading([]); }
    }
  }

  async function action(item: KnowledgeDocument, kind: 'delete' | 'retry') {
    if (busyId) return;
    if (kind === 'delete' && !window.confirm(`删除“${item.name}”及其文件和索引？历史回答会保留引用摘录。`)) return;
    setBusyId(item.id); setError('');
    try {
      if (kind === 'delete') await deleteKnowledgeDocument(item.id); else await retryKnowledgeDocument(item.id);
      if (mounted.current) { setReload((value) => value + 1); if (kind === 'delete') setSelected(null); }
    } catch (error) { if (mounted.current) setError(errorText(error)); }
    finally { if (mounted.current) setBusyId(null); }
  }

  async function saveName(event: FormEvent) {
    event.preventDefault();
    if (!detail || busyId || !editingName.trim()) return;
    setBusyId(detail.id);
    try { await renameKnowledgeDocument(detail.id, editingName.trim()); if (mounted.current) setReload((value) => value + 1); }
    catch (error) { if (mounted.current) setDetailError(errorText(error)); }
    finally { if (mounted.current) setBusyId(null); }
  }

  async function search(event: FormEvent) {
    event.preventDefault();
    if (!query.trim() || searching) return;
    searchRequest.current?.abort(); const controller = new AbortController(); searchRequest.current = controller;
    setSearching(true); setError('');
    try {
      const result = await searchKnowledge(query.trim(), controller.signal);
      if (!controller.signal.aborted) setSearchResults(result.citations);
    } catch (error) { if (!controller.signal.aborted) setError(errorText(error)); }
    finally { if (!controller.signal.aborted) setSearching(false); }
  }

  async function moreContent() {
    if (!selected || !content || content.next_offset == null || detailLoading) return;
    const current = selected; setDetailLoading(true);
    try {
      const next = await knowledgeContent(current, content.next_offset, detailRequest.current?.signal);
      if (selectedRef.current === current) setContent((old) => old ? { ...next, offset: 0, content: old.content + next.content } : next);
    } catch (error) { if (mounted.current && selectedRef.current === current) setDetailError(errorText(error)); }
    finally { if (mounted.current && selectedRef.current === current) setDetailLoading(false); }
  }

  return <main className="main-panel knowledge-panel">
    <header className="topbar"><div className="topbar-left">
      {showMenu && <button className="icon-button" onClick={onMenu} aria-label="展开侧栏"><PanelLeftOpen size={19} /></button>}
      <span className="topbar-title">知识库</span>
    </div><button className="text-button" onClick={onBack}><ArrowLeft size={14} />返回对话</button></header>
    <div className="knowledge-scroll">
      <section className="knowledge-body" onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
        onDragLeave={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node)) setDragging(false); }}
        onDrop={(event) => { event.preventDefault(); setDragging(false); void upload(Array.from(event.dataTransfer.files)); }}>
        <div className="knowledge-heading"><div><h1>我的知识库</h1><p>上传资料，开启知识库后即可用于对话。</p></div>
          <button className="knowledge-primary" onClick={() => fileRef.current?.click()} disabled={!summary || !!uploading.length}>
            <Upload size={16} />上传资料</button>
        </div>
        <input className="sr-only" type="file" multiple ref={fileRef} accept=".txt,.md,.markdown" aria-label="上传知识资料"
          onChange={(event) => { void upload(Array.from(event.target.files ?? [])); event.target.value = ''; }} />
        <input className="sr-only" type="file" ref={replacementRef} accept=".txt,.md,.markdown" aria-label="替换资料内容"
          onChange={(event) => { if (detail) void upload(Array.from(event.target.files ?? []), detail.id); event.target.value = ''; }} />
        {summary?.configuration_error && <div className="knowledge-alert" role="status">{summary.configuration_error}</div>}
        {listError && <div className="knowledge-alert" role="alert">{listError}<button className="text-button" onClick={() => setReload((value) => value + 1)}>重新加载</button></div>}
        {error && <div className="knowledge-alert" role="alert">{error}</div>}
        {notice && <div className="knowledge-notice" role="status"><Check size={14} />{notice}</div>}
        <div className="knowledge-toolbar"><form className="knowledge-search" onSubmit={search}>
          <Search size={16} /><input aria-label="查找知识资料" value={query} maxLength={200}
            onChange={(event) => { setQuery(event.target.value); setSearchResults(null); searchRequest.current?.abort(); setSearching(false); }} placeholder="查找资料或输入问题…" />
          {query && <button className="text-button" disabled={searching} type="submit">{searching ? '检索中…' : '按内容检索'}</button>}
        </form><span>{summary ? `${summary.total} 份资料 · ${summary.ready} 份可检索` : ''}</span></div>
        {!!uploading.length && <div className="knowledge-uploading" role="status"><span className="small-spinner" />正在上传 {uploading[0]}{uploading.length > 1 ? `，还有 ${uploading.length - 1} 份` : ''}</div>}
        {dragging && <div className="knowledge-drop">松开鼠标上传资料</div>}
        {searchResults ? <div className="knowledge-results"><div className="knowledge-result-title">相关内容 <button className="text-button" onClick={() => setSearchResults(null)}>返回资料列表</button></div>
          {searchResults.length ? searchResults.map((source) => <button className="knowledge-hit" key={source.chunk_id}
            onClick={() => setSelected({ documentId: source.document_id, versionId: source.version_id, chunkId: source.chunk_id })}>
            <strong><FileText size={16} />{source.title}</strong><small>{source.location}</small><p>{source.quote}</p>
          </button>) : <div className="knowledge-empty">没有找到相关内容</div>}
        </div> : <>
          {!!page.items.length && <div className="knowledge-list" aria-label="知识资料列表">{page.items.map((item) => <div className="knowledge-row" key={item.id}>
            <button className="knowledge-document" onClick={() => setSelected({ documentId: item.id })}>
              <span className="knowledge-file-icon"><FileText size={20} strokeWidth={1.4} /></span>
              <span><strong>{item.name}</strong><small>{fileSize(item.size)} · {item.chunk_count} 段 · {new Date(item.updated_at).toLocaleDateString('zh-CN')}{item.active_version_id && item.status !== 'ready' ? ' · 旧版可检索' : ''}</small>
                {item.error && <small className="knowledge-row-error">{item.error}</small>}</span>
            </button>
            <span className={`knowledge-status status-${item.status}`}>{processing(item) && <span className="small-spinner" />}
              {item.active_version_id && processing(item) && item.status !== 'deleting' ? '更新中' : statuses[item.status] || item.status}
              {item.status === 'indexing' && item.total > 0 ? ` ${item.completed}/${item.total}` : ''}</span>
            <div className="knowledge-row-actions">
              {['failed', 'update_failed'].includes(item.status) && <button className="icon-button" aria-label={`重试 ${item.name}`} onClick={() => void action(item, 'retry')} disabled={!!busyId}><RefreshCw size={15} /></button>}
              <button className="icon-button" aria-label={`${item.status === 'cleanup_failed' ? '重试清理' : '删除'} ${item.name}`}
                disabled={!!busyId || item.status === 'deleting'} onClick={() => void action(item, 'delete')}><Trash2 size={15} /></button>
            </div>
          </div>)}</div>}
          {!page.items.length && <div className="knowledge-empty"><BookOpen size={34} strokeWidth={1.1} />
            <h2>{loading ? '正在加载…' : query ? '没有同名资料' : '把资料放在这里'}</h2>
            {!loading && <p>{query ? '可以按内容检索，试试问一个问题。' : '支持 TXT、Markdown，也可以直接拖入文件。'}</p>}</div>}
          {page.next_cursor && <button className="knowledge-more text-button" disabled={loading} onClick={() => {
            const controller = new AbortController(); listRequest.current?.abort(); listRequest.current = controller; setLoading(true);
            void load(controller.signal, page.next_cursor).catch((error) => { if (!controller.signal.aborted) setListError(errorText(error)); })
              .finally(() => { if (!controller.signal.aborted) setLoading(false); });
          }}>加载更多<ChevronRight size={14} /></button>}
        </>}
      </section>
    </div>
    {selected && <dialog ref={dialogRef} className="knowledge-detail" aria-label="资料详情" onCancel={() => setSelected(null)}
      onClick={(event) => { if (event.target === event.currentTarget && event.clientX < event.currentTarget.getBoundingClientRect().left) setSelected(null); }}>
        <header><h2>{detail?.name || '资料详情'}</h2><button className="icon-button" onClick={() => setSelected(null)} aria-label="关闭资料详情"><X size={18} /></button></header>
        {detail && <><form className="knowledge-rename" onSubmit={saveName}><input aria-label="资料名称" value={editingName} onChange={(event) => setEditingName(event.target.value)} maxLength={200} />
          <button className="text-button" disabled={!!busyId || editingName === detail.name || !editingName.trim()}>保存名称</button></form>
          <div className="knowledge-detail-actions"><a className="text-button" href={`/api/knowledge/documents/${detail.id}/file`}><Download size={14} />{selected.versionId && selected.versionId !== detail.active_version_id ? '当前版本原文件' : '原文件'}</a>
            <button className="text-button" disabled={processing(detail) || !!uploading.length} onClick={() => replacementRef.current?.click()}>更新内容</button>
            <button className="text-button" disabled={processing(detail) || !!busyId} onClick={() => void action(detail, 'retry')}><RefreshCw size={13} />重新处理</button></div></>}
        {detailError && <div className="knowledge-alert" role="alert">{detailError}</div>}
        {selected.snapshot && <div className="knowledge-snapshot">
          <div className="knowledge-preview-label">回答时的引用 · {selected.snapshot.title} · {selected.snapshot.location}</div>
          <pre className="knowledge-content">{selected.snapshot.quote}</pre>
        </div>}
        {detailLoading && <p className="knowledge-detail-loading">正在读取资料…</p>}
        {content && <><div className="knowledge-preview-label">{content.is_current ? '提取正文' : '历史版本，当前资料已有更新'}</div>
          <pre className="knowledge-content">{content.content}</pre>
          {content.next_offset != null && <button className="text-button" disabled={detailLoading} onClick={() => void moreContent()}>继续读取正文</button>}</>}
        {chunks && <details className="knowledge-chunks" open={!!selected.chunkId}><summary>查看分段（当前页 {chunks.items.length} 段）</summary>
          {chunks.items.map((chunk) => <div key={chunk.id} id={`knowledge-chunk-${chunk.id}`} className={`knowledge-chunk ${chunk.id === selected.chunkId ? 'is-highlighted' : ''}`}>
            <strong>{chunk.location}</strong><pre>{chunk.content}</pre></div>)}
          {chunks.next_offset != null && <button className="text-button" onClick={async () => {
            const current = selected;
            try { const next = await knowledgeChunks(current, chunks.next_offset!, detailRequest.current?.signal);
              if (selectedRef.current === current) setChunks((old) => old ? { ...next, items: [...old.items, ...next.items] } : next);
            } catch (error) { if (selectedRef.current === current) setDetailError(errorText(error)); }
          }}>更多分段</button>}</details>}
    </dialog>}
  </main>;
}
