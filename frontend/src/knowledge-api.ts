/** 知识库接口只传文件、问题和资料编号，用户归属由后端判断。 */
import { ApiError, type Citation } from './api';

export type KnowledgeDocument = {
  id: string; name: string; source_type: string; status: string; error: string | null;
  active_version_id: string | null; target_version_id: string | null;
  size: number; chunk_count: number; completed: number; total: number; job_id: string | null; updated_at: string;
};
export type KnowledgePage = { items: KnowledgeDocument[]; next_cursor: string | null };
export type KnowledgeSummary = { total: number; ready: number; processing: number;
  configuration_error: string | null; max_file_size: number; max_batch_files: number; supported_formats: string[] };
export type KnowledgeTarget = { documentId: string; versionId?: string; chunkId?: string; snapshot?: Citation };
export type ContentPage = { document_id: string; version_id: string; name: string; is_current: boolean;
  offset: number; content: string; total: number; next_offset: number | null };
export type ChunkPage = { items: { id: string; location: string; start: number; end: number; content: string }[];
  next_offset: number | null };

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api/knowledge${path}`, { credentials: 'same-origin', cache: 'no-store', ...options });
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new ApiError(typeof data?.detail === 'string' ? data.detail : '知识资料请求失败，请重试', response.status);
  return data as T;
}

export const knowledgeSummary = (signal?: AbortSignal) => request<KnowledgeSummary>('', { signal });
export const knowledgeDocuments = (query: string, cursor?: string | null, signal?: AbortSignal) =>
  request<KnowledgePage>(`/documents?${new URLSearchParams({ query, ...(cursor ? { cursor } : {}) })}`, { signal });
export const knowledgeDocument = (id: string, signal?: AbortSignal) => request<KnowledgeDocument>(`/documents/${id}`, { signal });
export const deleteKnowledgeDocument = (id: string) => request<KnowledgeDocument>(`/documents/${id}`, { method: 'DELETE' });
export const retryKnowledgeDocument = (id: string) => request<KnowledgeDocument>(`/documents/${id}/reprocess`, { method: 'POST' });
export const renameKnowledgeDocument = (id: string, name: string) => request<KnowledgeDocument>(`/documents/${id}`, {
  method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }),
});

/** 普通导入与显式更新分别使用不同接口，同名上传不会静默覆盖。 */
export const uploadKnowledgeDocument = (file: File, signal: AbortSignal, documentId?: string) => {
  const body = new FormData(); body.append('file', file);
  return request<KnowledgeDocument>(documentId ? `/documents/${documentId}/versions` : '/documents', { method: 'POST', body, signal });
};
export const knowledgeContent = (target: KnowledgeTarget, offset: number, signal?: AbortSignal) =>
  request<ContentPage>(`/documents/${target.documentId}/content?${new URLSearchParams({ offset: String(offset),
    ...(target.versionId ? { version_id: target.versionId } : {}) })}`, { signal });
export const knowledgeChunks = (target: KnowledgeTarget, offset = 0, signal?: AbortSignal) =>
  request<ChunkPage>(`/documents/${target.documentId}/chunks?${new URLSearchParams({ offset: String(offset),
    ...(target.chunkId && offset === 0 ? { chunk_id: target.chunkId } : {}),
    ...(target.versionId ? { version_id: target.versionId } : {}) })}`, { signal });
export const searchKnowledge = (query: string, signal?: AbortSignal) => request<{ citations: Citation[] }>('/retrieve', {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ query }), signal,
});
