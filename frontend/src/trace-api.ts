/** 调用详情按需获取，不增加聊天列表和流式响应的体积。 */
import { request, type Attachment, type Citation } from './api';

export type TraceTarget = { conversationId: string; requestId: string };
export type ModelCallTrace = {
  id: string; sequence: number; model: string; status: string;
  request_body: Record<string, unknown> | null;
  images: { attachment: Attachment; sha256: string }[];
  capture_error: string | null; http_status: number | null;
  error: string | null; elapsed_ms: number | null; created_at: string; finished_at: string | null;
};
export type TraceAttempt = {
  id: string; http_request_id: string; model: string; knowledge_enabled: boolean;
  status: string; created_at: string; finished_at: string | null;
  retrieval: { query: string; elapsed_ms: number | null; citations: Citation[]; error: string | null; message: string } | null;
  calls: ModelCallTrace[];
};
export type RequestTrace = { conversation_id: string; request_id: string; attempts: TraceAttempt[] };

function tracePath(target: TraceTarget) {
  return `/conversations/${encodeURIComponent(target.conversationId)}/requests/${encodeURIComponent(target.requestId)}/trace`;
}

export const getRequestTrace = (target: TraceTarget, signal: AbortSignal) =>
  request<RequestTrace>(tracePath(target), { signal: AbortSignal.any([signal, AbortSignal.timeout(15000)]) });

/** 复制和下载时才还原图片，普通详情不传大段 Base64。 */
export const getOriginalModelRequest = (target: TraceTarget, callId: string, signal: AbortSignal) =>
  request<Record<string, unknown>>(`${tracePath(target)}/calls/${encodeURIComponent(callId)}/request`, {
    signal: AbortSignal.any([signal, AbortSignal.timeout(15000)]),
  });
