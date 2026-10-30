/** 本地个人资料只含昵称，不代表已登录用户。 */
export type User = { id: number; display_name: string };
export type Attachment = { id: string; name: string; media_type: string; size: number; url: string };
export type ToolCall = {
  id: string;
  tool: string;
  query: string;
  status: 'running' | 'completed' | 'error' | 'cancelled';
  sources: { title: string; url: string }[];
  error?: string | null;
};
export type StreamProgress = { type: 'answer_reset' } | { type: 'tool'; call: ToolCall };
export type Message = {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  requestId?: string;
  model?: string | null;
  status?: 'pending' | 'error' | 'cancelled';
  notice?: string;
  toolCalls?: ToolCall[];
  attachments?: Attachment[];
};
export type Conversation = {
  id: string;
  title: string;
  modelName: string;
  imageInputSupported?: boolean | null;
  updatedAt: number;
  messages: Message[];
  generating?: boolean;
};

type SavedMessage = { id: string; role: 'user' | 'assistant'; content: string; request_id: string;
  model: string | null; attachments?: Attachment[] };
type SavedConversation = {
  id: string; title: string; model_name: string; image_input_supported?: boolean | null; updated_at: string; generating: boolean;
  messages: SavedMessage[];
};

export type ConversationTurn = {
  requestId: string; conversationId: string; updatedAt: number; messages: Message[];
};

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

/** 所有请求由同源 /api 代理转发，不在前端保存模型密钥。 */
async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...options, credentials: 'same-origin', cache: 'no-store',
    headers: { 'Content-Type': 'application/json', ...options.headers },
  });
  if (response.status === 204) return undefined as T;
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = typeof data?.detail === 'string' ? data.detail :
      response.status === 422 ? '输入格式不正确，请检查后重试。' : `服务暂时不可用（${response.status}）`;
    throw new ApiError(detail, response.status);
  }
  return data as T;
}

/** 将数据库接口的时间和字段名转成页面使用的结构。 */
function toConversation(data: SavedConversation): Conversation {
  return {
    id: data.id, title: data.title, modelName: data.model_name, imageInputSupported: data.image_input_supported,
    updatedAt: Date.parse(data.updated_at), generating: data.generating,
    messages: data.messages.map((message) => ({
      id: message.id, role: message.role, content: message.content,
      requestId: message.request_id, model: message.model,
      attachments: message.attachments ?? [],
    })),
  };
}

export const getProfile = (signal?: AbortSignal) => request<User>('/profile', {
  signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(15000)]) : AbortSignal.timeout(15000),
});
export const updateProfile = (display_name: string) => request<User>('/profile', {
  method: 'PUT', body: JSON.stringify({ display_name }),
});

/** 上传使用 FormData，让浏览器自动生成 multipart 边界，不能手动设置 JSON 请求头。 */
export async function uploadImage(file: File, signal: AbortSignal): Promise<Attachment> {
  const form = new FormData();
  form.append('file', file);
  const response = await fetch('/api/attachments', {
    method: 'POST', body: form, signal, credentials: 'same-origin', cache: 'no-store',
  });
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new ApiError(typeof data?.detail === 'string' ? data.detail : '图片上传失败，请重试。', response.status);
  if (!data || typeof data.id !== 'string' || typeof data.name !== 'string' ||
      !['image/jpeg', 'image/png', 'image/webp'].includes(data.media_type) || typeof data.size !== 'number') {
    throw new Error('图片上传结果不正确，请重试。');
  }
  return data as Attachment;
}

/** 图片走相同的后端代理，既不依赖浏览器本地文件，也不暴露服务器磁盘路径。 */
export const attachmentUrl = (attachment: Attachment) => `/api/attachments/${encodeURIComponent(attachment.id)}`;

export type ModelOption = { model: string; image_input_supported: boolean | null };
export type ModelCatalog = { default_model: string; models: ModelOption[] };

/** 可选模型和图片能力统一由后端配置提供，前端不维护模型名白名单。 */
export const getModels = (signal: AbortSignal) => request<ModelCatalog>('/models', { signal });

/** 只返回模型字段，调用方合并到原会话，避免覆盖消息和标题。 */
export const switchConversationModel = (id: string, model_name: string) =>
  request<{ id: string; model_name: string; image_input_supported: boolean | null }>(`/conversations/${id}/model`, {
    method: 'PATCH', body: JSON.stringify({ model_name }), signal: AbortSignal.timeout(15000),
  });

export async function listConversations(signal?: AbortSignal): Promise<Conversation[]> {
  return (await request<SavedConversation[]>('/conversations', { signal })).map(toConversation);
}
/** 首次问题交给后端生成并保存标题，页面直接使用接口返回的标题。 */
export async function createConversation(firstMessage: string, modelName?: string): Promise<Conversation> {
  return toConversation(await request<SavedConversation>('/conversations', {
    method: 'POST', body: JSON.stringify({ first_message: firstMessage, model_name: modelName || undefined }),
  }));
}
export async function getConversation(id: string, signal?: AbortSignal): Promise<Conversation> {
  return toConversation(await request<SavedConversation>(`/conversations/${id}`, {
    signal: signal ?? AbortSignal.timeout(15000),
  }));
}
export const removeConversation = (id: string) => request<void>(`/conversations/${id}`, { method: 'DELETE' });

/** 标题由服务端保存，刷新页面后仍以数据库中的值为准。 */
export async function renameConversation(id: string, title: string): Promise<Conversation> {
  return toConversation(await request<SavedConversation>(`/conversations/${id}`, {
    method: 'PATCH', body: JSON.stringify({ title }), signal: AbortSignal.timeout(15000),
  }));
}

/** 解析一个完整 SSE 事件；注释是心跳，文字片段立即交给聊天页面。 */
function readStreamEvent(block: string, id: string, requestId: string,
  onDelta: (text: string) => void, onProgress?: (event: StreamProgress) => void): ConversationTurn | undefined {
  const lines = block.split(/\r?\n/);
  const event = lines.find((line) => line.startsWith('event:'))?.slice(6).trim();
  const payload = lines.filter((line) => line.startsWith('data:'))
    .map((line) => line.slice(5).replace(/^ /, '')).join('\n');
  if (!payload || !['delta', 'done', 'error', 'answer_reset', 'tool_start', 'tool_end'].includes(event ?? '')) return;
  const data = JSON.parse(payload);
  if (data.request_id !== requestId) throw new Error('回答请求编号不匹配，请重新打开会话。');
  if (event === 'error') throw new Error(typeof data.message === 'string' ? data.message : '生成失败，请重试。');
  if (event === 'answer_reset') {
    onProgress?.({ type: 'answer_reset' });
    return;
  }
  if (event === 'tool_start' || event === 'tool_end') {
    if (typeof data.call_id !== 'string' || typeof data.tool !== 'string' || typeof data.query !== 'string') {
      throw new Error('工具调用数据格式不正确，请重试。');
    }
    if (event === 'tool_end' && (!['completed', 'error'].includes(data.status) || !Array.isArray(data.sources) ||
      data.sources.some((source: { title: string; url: string }) => !source ||
        typeof source.title !== 'string' || typeof source.url !== 'string' || !/^https?:\/\//i.test(source.url)) ||
      (data.error != null && typeof data.error !== 'string'))) {
      throw new Error('工具执行结果格式不正确，请重试。');
    }
    onProgress?.({ type: 'tool', call: { id: data.call_id, tool: data.tool, query: data.query,
      status: event === 'tool_start' ? 'running' : data.status,
      sources: event === 'tool_start' ? [] : data.sources, error: data.error } });
    return;
  }
  if (event === 'delta') {
    if (typeof data.text !== 'string') throw new Error('回答片段格式不正确，请重试。');
    onDelta(data.text);
    return;
  }
  if (data.conversation_id !== id || !Array.isArray(data.messages) || data.messages.length !== 2 ||
    data.generating !== false || !Number.isFinite(Date.parse(data.updated_at)) ||
    data.messages.some((item: SavedMessage, index: number) => !item || typeof item.id !== 'string' ||
      typeof item.content !== 'string' || item.request_id !== requestId || item.role !== (index ? 'assistant' : 'user'))) {
    throw new Error('回答完成数据不正确，请重新打开会话。');
  }
  return {
    requestId, conversationId: id, updatedAt: Date.parse(data.updated_at),
    messages: data.messages.map((item: SavedMessage) => ({
      id: item.id, role: item.role, content: item.content, requestId: item.request_id, model: item.model,
      attachments: item.attachments ?? [],
    })),
  };
}

/** 用 POST 读取流，缓冲跨网络分块的事件和 UTF-8 字符，只在收到 done 后完成。 */
export async function streamMessage(id: string, message: string, requestId: string,
  signal: AbortSignal, onDelta: (text: string) => void,
  onProgress?: (event: StreamProgress) => void, attachments: Attachment[] = []): Promise<ConversationTurn> {
  const response = await fetch(`/api/conversations/${id}/messages/stream`, {
    method: 'POST', credentials: 'same-origin', cache: 'no-store', signal,
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify({ message, request_id: requestId, attachment_ids: attachments.map((item) => item.id) }),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    throw new ApiError(typeof data?.detail === 'string' ? data.detail : '发送失败，请重试。', response.status);
  }
  if (!response.body || !response.headers.get('content-type')?.includes('text/event-stream')) {
    throw new Error('服务未返回流式回答，请检查后端版本。');
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8', { fatal: true });
  let buffer = '';
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      let boundary: RegExpMatchArray | null;
      while ((boundary = buffer.match(/\r?\n\r?\n/))) {
        const index = boundary.index!;
        const block = buffer.slice(0, index);
        buffer = buffer.slice(index + boundary[0].length);
        const result = readStreamEvent(block, id, requestId, onDelta, onProgress);
        if (result) return result;
      }
      if (done) throw new Error('连接已中断，正在核对回答是否保存。');
    }
  } finally {
    await reader.cancel().catch(() => { /* 连接可能已因停止或网络故障关闭。 */ });
    reader.releaseLock();
  }
}
