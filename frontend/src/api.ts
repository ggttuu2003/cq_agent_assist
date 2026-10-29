/** 请求现有的单轮对话接口，模型地址和密钥仍由 Python 后端管理。 */
export async function sendMessage(message: string, signal: AbortSignal): Promise<string> {
  const response = await fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message }),
    signal,
  });

  const data: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = data && typeof data === 'object' && 'detail' in data ? data.detail : null;
    if (typeof detail === 'string') throw new Error(detail);
    if (response.status === 422) throw new Error('消息格式不正确，请修改后重新发送。');
    throw new Error(`暂时无法获取回答（${response.status}），请检查后端是否已启动。`);
  }

  if (
    !data ||
    typeof data !== 'object' ||
    !('reply' in data) ||
    typeof data.reply !== 'string' ||
    !data.reply.trim()
  ) {
    throw new Error('没有收到有效的回答，请重试。');
  }
  return data.reply;
}
