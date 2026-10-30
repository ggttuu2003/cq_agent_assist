import {
  lazy,
  Suspense,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type FormEvent,
  type KeyboardEvent,
  type PointerEvent,
} from 'react';
import {
  ArrowDown,
  ArrowUp,
  ArrowUpRight,
  Check,
  Copy,
  Cpu,
  Pencil,
  MessageSquare,
  UserRound,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  RotateCcw,
  Search,
  Square,
  SquarePen,
  Sun,
  Trash2,
  X,
} from 'lucide-react';
import { createConversation, getConversation, getDefaultModel, listConversations, removeConversation, renameConversation, sendMessage, type Conversation, type Message, type User } from './api';

// Markdown 和代码高亮体积较大，首次出现回答时再加载，欢迎页可以更快显示。
const Markdown = lazy(() => import('./Markdown'));

const SIDEBAR_WIDTH_KEY = 'cq-assist.sidebar-width';
const SIDEBAR_MIN_WIDTH = 220;
const SIDEBAR_DEFAULT_WIDTH = 256;
const SIDEBAR_MAX_WIDTH = 400;

/** 恢复用户设置的侧栏宽度；无记录或记录无效时使用默认宽度。 */
function loadSidebarWidth(): number {
  try {
    const saved = Number(localStorage.getItem(SIDEBAR_WIDTH_KEY) ?? NaN);
    return Number.isFinite(saved)
      ? Math.min(SIDEBAR_MAX_WIDTH, Math.max(SIDEBAR_MIN_WIDTH, saved))
      : SIDEBAR_DEFAULT_WIDTH;
  } catch {
    return SIDEBAR_DEFAULT_WIDTH;
  }
}

/** 生成本轮发送编号，重试时继续使用，服务端据此避免重复保存。 */
function createId() {
  return crypto.randomUUID();
}

/** 用自然日期给侧栏分组，历史会话由本地个人档案的数据库记录提供。 */
function conversationGroup(timestamp: number) {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  if (timestamp >= today.getTime()) return '今天';
  const yesterday = new Date(today);
  yesterday.setDate(yesterday.getDate() - 1);
  return timestamp >= yesterday.getTime() ? '昨天' : '更早';
}

/** 简单的品牌图形，保持与页面一致的单色风格。 */
function BrandMark({ className = '' }: { className?: string }) {
  return (
    <span className={`brand-mark ${className}`} aria-hidden="true">
      <svg viewBox="0 0 40 40" fill="none">
        <path
          d="M26.5 13a10 10 0 1 0 0 14M26.5 13v14M22 20h9"
          stroke="currentColor"
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </span>
  );
}

/** 聊天工作区：读取本地个人档案的会话，模型上下文由服务端组装。 */
export default function ChatWorkspace({ user, profileLoading, profileError, onRetryProfile, onEditProfile }: {
  user: User | null;
  profileLoading: boolean;
  profileError: string;
  onRetryProfile: () => void;
  onEditProfile: () => void;
}) {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [historyError, setHistoryError] = useState('');
  const [creating, setCreating] = useState(false);
  const [renaming, setRenaming] = useState<{ id: string; title: string; error: string } | null>(null);
  const [savingTitle, setSavingTitle] = useState(false);
  const [draft, setDraft] = useState('');
  const [defaultModelName, setDefaultModelName] = useState<string | null>(null);
  const [defaultModelError, setDefaultModelError] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(() => window.innerWidth > 760);
  const [sidebarWidth, setSidebarWidth] = useState(loadSidebarWidth);
  const [viewportWidth, setViewportWidth] = useState(() => window.innerWidth);
  const [resizingSidebar, setResizingSidebar] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [search, setSearch] = useState('');
  const [theme, setTheme] = useState(() => document.documentElement.dataset.theme ?? 'light');
  const [pending, setPending] = useState<{ conversationId: string; messageId: string } | null>(
    null,
  );
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [toast, setToast] = useState('');
  const [showScrollButton, setShowScrollButton] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const searchTriggerRef = useRef<HTMLButtonElement>(null);
  const searchDialogRef = useRef<HTMLDialogElement>(null);
  const requestRef = useRef<AbortController | null>(null);
  const shouldFollowRef = useRef(true);
  const composingRef = useRef(false);
  const sidebarDragRef = useRef<{ pointerId: number; startX: number; startWidth: number } | null>(
    null,
  );
  // 至少为聊天区保留 480 像素；窗口缩小时临时收窄，保留用户原本的宽度偏好。
  const maxSidebarWidth = Math.min(
    SIDEBAR_MAX_WIDTH,
    Math.max(SIDEBAR_MIN_WIDTH, viewportWidth - 480),
  );
  const visibleSidebarWidth = Math.min(maxSidebarWidth, sidebarWidth);
  const active = conversations.find((item) => item.id === activeId);
  const messages = active?.messages ?? [];
  const isWelcome = messages.length === 0;
  // 打开会话时以该会话为准，默认模型接口失败也不影响已有会话的显示。
  const displayedModel = active
    ? active.modelName || '未配置模型'
    : defaultModelError ? '模型信息不可用' : defaultModelName ?? '读取模型…';
  const isActivePending = pending?.conversationId === activeId;
  const filteredConversations = [...conversations]
    .sort((first, second) => second.updatedAt - first.updatedAt)
    .filter(
      (item) =>
        !search.trim() ||
        `${item.title} ${item.messages.map((message) => message.content).join(' ')}`
          .toLocaleLowerCase()
          .includes(search.trim().toLocaleLowerCase()),
    );

  useEffect(() => {
    // 默认模型仅用于新会话，已有会话的模型来自会话列表或详情。
    const controller = new AbortController();
    getDefaultModel(controller.signal)
      .then((name) => {
        if (!controller.signal.aborted) {
          setDefaultModelName(name || '未配置模型');
          setDefaultModelError(false);
        }
      })
      .catch(() => {
        if (!controller.signal.aborted) setDefaultModelError(true);
      });
    return () => controller.abort();
  }, [user?.id]);

  useEffect(() => {
    function handleWindowResize() {
      setViewportWidth(window.innerWidth);
      if (window.innerWidth <= 760) {
        sidebarDragRef.current = null;
        setResizingSidebar(false);
      }
    }
    window.addEventListener('resize', handleWindowResize);
    return () => window.removeEventListener('resize', handleWindowResize);
  }, []);

  useEffect(() => {
    // 拖动结束后再保存，避免移动鼠标时频繁写入本地存储。
    if (resizingSidebar) return;
    try {
      localStorage.setItem(SIDEBAR_WIDTH_KEY, String(sidebarWidth));
    } catch {
      /* 无法保存偏好时，宽度调整仍在当前页面生效。 */
    }
  }, [sidebarWidth, resizingSidebar]);

  /** 重新读取个人档案下的全部会话，不触碰旧版匿名浏览器记录。 */
  async function reloadConversations() {
    setHistoryLoading(true);
    setHistoryError('');
    try {
      setConversations(await listConversations());
    } catch {
      setHistoryError('会话加载失败，请检查服务后重试。');
    } finally { setHistoryLoading(false); }
  }

  useEffect(() => {
    if (!user) return;
    setHistoryLoading(true);
    setHistoryError('');
    const controller = new AbortController();
    listConversations(controller.signal).then((items) => {
      if (!controller.signal.aborted) setConversations(items);
    }).catch(() => {
      if (!controller.signal.aborted) setHistoryError('会话加载失败，请检查服务后重试。');
    }).finally(() => {
      if (!controller.signal.aborted) setHistoryLoading(false);
    });
    return () => controller.abort();
  }, [user?.id]);

  // 页面停止等待或重新打开时，继续读取服务器生成状态，恢复已经保存的回答。
  useEffect(() => {
    if (!active?.generating || pending?.conversationId === active.id) return;
    const id = active.id;
    const controller = new AbortController();
    const timer = window.setInterval(() => {
      getConversation(id, controller.signal).then((saved) => {
        if (!controller.signal.aborted && !saved.generating) {
          setConversations((items) => items.map((item) => {
            if (item.id !== id) return item;
            const waiting = item.messages.at(-1);
            // 后台未保存成功时，保留页面上的原问题和重试入口。
            if (waiting?.status && !saved.messages.some((message) => message.requestId === waiting.id)) {
              return { ...item, generating: false };
            }
            return saved;
          }));
        }
      }).catch(() => { /* 短暂网络失败时保留页面，下一次轮询继续恢复。 */ });
    }, 2000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [active?.id, active?.generating, pending?.conversationId]);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem('cq-assist.theme', theme);
    } catch {
      /* 不支持本地存储时，主题仍在本次访问中生效。 */
    }
  }, [theme]);

  useEffect(() => {
    const textarea = inputRef.current;
    if (!textarea) return;
    textarea.style.height = '0px';
    textarea.style.height = `${Math.min(textarea.scrollHeight, 180)}px`;
  }, [draft, isWelcome]);

  useEffect(() => {
    if (shouldFollowRef.current)
      scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
  }, [messages.length, messages.at(-1)?.content, messages.at(-1)?.status]);

  useEffect(() => {
    shouldFollowRef.current = true;
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
    setShowScrollButton(false);
  }, [activeId]);

  useEffect(() => {
    const dialog = searchDialogRef.current;
    if (searchOpen && dialog && !dialog.open) {
      dialog.showModal();
      searchRef.current?.focus();
    } else if (!searchOpen && dialog?.open) {
      dialog.close();
      searchTriggerRef.current?.focus();
    }
  }, [searchOpen]);

  useEffect(() => {
    function handleShortcut(event: globalThis.KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        setSearchOpen((value) => !value);
      }
      if (event.key === 'Escape')
        setSidebarOpen((value) => (window.innerWidth <= 760 ? false : value));
    }
    document.addEventListener('keydown', handleShortcut);
    return () => document.removeEventListener('keydown', handleShortcut);
  }, []);

  useEffect(() => {
    if (!toast) return;
    const timeout = window.setTimeout(() => setToast(''), 3500);
    return () => window.clearTimeout(timeout);
  }, [toast]);

  useEffect(() => () => requestRef.current?.abort(), []);

  /** 捕获指针，鼠标离开细小拖动条后仍可继续调整侧栏。 */
  function startSidebarResize(event: PointerEvent<HTMLDivElement>) {
    if (event.button !== 0 || viewportWidth <= 760) return;
    event.preventDefault();
    event.currentTarget.focus();
    event.currentTarget.setPointerCapture(event.pointerId);
    sidebarDragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startWidth: visibleSidebarWidth,
    };
    setResizingSidebar(true);
  }

  /** 根据本次拖动的起点计算宽度，并限制在当前窗口允许的范围内。 */
  function moveSidebarResize(event: PointerEvent<HTMLDivElement>) {
    const drag = sidebarDragRef.current;
    if (!drag || event.pointerId !== drag.pointerId) return;
    setSidebarWidth(
      Math.min(
        maxSidebarWidth,
        Math.max(SIDEBAR_MIN_WIDTH, drag.startWidth + event.clientX - drag.startX),
      ),
    );
  }

  /** 松开、取消或失去指针捕获时结束拖动，恢复正常的光标和文字选择。 */
  function finishSidebarResize() {
    sidebarDragRef.current = null;
    setResizingSidebar(false);
  }

  /** 允许聚焦分隔条后用方向键调整，Home/End 跳到最窄或最宽。 */
  function handleSidebarResizeKey(event: KeyboardEvent<HTMLDivElement>) {
    const step = event.shiftKey ? 30 : 10;
    let width: number;
    switch (event.key) {
      case 'ArrowLeft':
        width = visibleSidebarWidth - step;
        break;
      case 'ArrowRight':
        width = visibleSidebarWidth + step;
        break;
      case 'Home':
        width = SIDEBAR_MIN_WIDTH;
        break;
      case 'End':
        width = maxSidebarWidth;
        break;
      default:
        return;
    }
    event.preventDefault();
    setSidebarWidth(Math.min(maxSidebarWidth, Math.max(SIDEBAR_MIN_WIDTH, width)));
  }

  /** 按指定会话更新回答，切换侧栏时也不会把结果写进另一段对话。 */
  function updateAnswer(
    conversationId: string,
    messageId: string,
    content: string,
    status?: Message['status'],
  ) {
    setConversations((previous) =>
      previous.map((conversation) =>
        conversation.id === conversationId
          ? {
              ...conversation,
              messages: conversation.messages.map((message) =>
                message.id === messageId ? { ...message, content, status } : message,
              ),
            }
          : conversation,
      ),
    );
  }

  /** 发送会话编号和请求编号；服务端负责读取历史并保存完整问答。 */
  async function requestAnswer(conversationId: string, messageId: string, question: string) {
    const controller = new AbortController();
    requestRef.current = controller;
    setPending({ conversationId, messageId });
    try {
      const saved = await sendMessage(conversationId, question, messageId, controller.signal);
      if (controller.signal.aborted) return;
      setConversations((items) => items.map((item) => item.id === saved.id ? saved : item));
    } catch (error) {
      if (controller.signal.aborted) {
        updateAnswer(conversationId, messageId, '已停止等待，后台可能仍在回答。', 'cancelled');
      } else {
        const detail = error instanceof TypeError ? '无法连接服务，请检查后重试。' :
          error instanceof Error ? error.message : '暂时无法获取回答，请重试。';
        updateAnswer(conversationId, messageId, detail, 'error');
      }
      // 响应丢失时数据库可能已经保存成功，先核对，避免让用户重复发送。
      try {
        const saved = await getConversation(conversationId, AbortSignal.timeout(8000));
        setConversations((items) => items.map((item) => {
          if (item.id !== saved.id) return item;
          if (saved.messages.some((message) => message.requestId === messageId)) return saved;
          return { ...item, generating: saved.generating };
        }));
      } catch { /* 未能确认结果时仍保留原问题和重试入口。 */ }
    } finally {
      if (requestRef.current === controller) {
        requestRef.current = null;
        setPending(null);
      }
    }
  }

  /** 第一次发送时创建服务端会话，创建失败时保留输入内容。 */
  async function submitMessage(event: FormEvent) {
    event.preventDefault();
    const question = draft.trim();
    if (!question || !user || pending || requestRef.current || historyLoading || historyError || active?.generating || (activeId && renaming?.id === activeId)) return;
    const controller = new AbortController();
    requestRef.current = controller;
    setCreating(true);
    try {
      const conversation = active ?? await createConversation(question);
      if (controller.signal.aborted) return;
      const answerId = createId();
      const additions: Message[] = [
        { id: createId(), role: 'user', content: question },
        { id: answerId, role: 'assistant', content: '', status: 'pending' },
      ];
      const updated = { ...conversation,
        updatedAt: Date.now(), messages: [...conversation.messages, ...additions] };
      setConversations((previous) => active ? previous.map((item) => item.id === updated.id ? updated : item) : [updated, ...previous]);
      shouldFollowRef.current = true;
      setActiveId(conversation.id);
      setDraft('');
      inputRef.current?.focus();
      void requestAnswer(conversation.id, answerId, question);
    } catch (error) {
      setToast(error instanceof Error ? error.message : '无法创建会话，请重试。');
    } finally {
      setCreating(false);
      if (requestRef.current === controller) requestRef.current = null;
    }
  }

  /** 中文输入法选词时的回车不发送；Shift+Enter 保留换行。 */
  function handleInputKey(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (
      event.key === 'Enter' &&
      !event.shiftKey &&
      !event.nativeEvent.isComposing &&
      !composingRef.current &&
      event.keyCode !== 229
    ) {
      event.preventDefault();
      if (draft.trim() && !pending) event.currentTarget.form?.requestSubmit();
    }
  }

  /** 新建空白页面，直到发送第一条消息时才保存会话。 */
  function newConversation() {
    setActiveId(null);
    setDraft('');
    if (window.innerWidth <= 760) setSidebarOpen(false);
    window.requestAnimationFrame(() => inputRef.current?.focus());
  }

  /** 切换时核对服务端最新记录，不调用模型。 */
  async function openConversation(id: string) {
    setActiveId(id);
    setDraft('');
    setSearchOpen(false);
    setSearch('');
    if (window.innerWidth <= 760) setSidebarOpen(false);
    const local = conversations.find((item) => item.id === id);
    if (pending?.conversationId === id || local?.messages.some((message) => message.status)) return;
    try {
      const saved = await getConversation(id);
      // 读取期间如果已经发送问题或更新回答，就不能用旧快照覆盖页面。
      setConversations((items) => items.map((item) =>
        item.id === id && item === local ? saved : item,
      ));
    } catch (error) { setToast(error instanceof Error ? error.message : '无法加载会话'); }
  }

  /** 在侧栏原位编辑标题，不切换当前正在查看的会话。 */
  function startRename(conversation: Conversation) {
    if (savingTitle || conversation.generating || pending?.conversationId === conversation.id) return;
    setRenaming({ id: conversation.id, title: conversation.title, error: '' });
  }

  /** 保存成功后只更新标题和时间，保留页面上正在等待或重试的消息。 */
  async function saveTitle(event: FormEvent) {
    event.preventDefault();
    if (!renaming || savingTitle) return;
    const title = renaming.title.trim();
    if (!title || Array.from(title).length > 120) {
      setRenaming({ ...renaming, error: '标题需要 1–120 个字符。' });
      return;
    }
    setSavingTitle(true);
    try {
      const saved = await renameConversation(renaming.id, title);
      setConversations((items) => items.map((item) => item.id === saved.id
        ? { ...item, title: saved.title, updatedAt: saved.updatedAt } : item));
      setRenaming(null);
      setToast('标题已修改');
    } catch (error) {
      setRenaming({ ...renaming, error: error instanceof Error ? error.message : '保存失败，请重试。' });
    } finally { setSavingTitle(false); }
  }

  /** 服务端删除成功之后再移除侧栏记录。 */
  async function deleteConversation(id: string) {
    if (pending?.conversationId === id) { setToast('会话正在回答，请完成后再删除。'); return; }
    if (!window.confirm('删除这段会话及全部消息？')) return;
    try {
      await removeConversation(id);
      setConversations((previous) => previous.filter((item) => item.id !== id));
      if (activeId === id) { setActiveId(null); setDraft(''); }
      setToast('已删除这段会话');
    } catch (error) { setToast(error instanceof Error ? error.message : '删除失败，请重试'); }
  }

  /** 将原始回答复制为文本，保留 Markdown 格式。 */
  async function copyMessage(message: Message) {
    try {
      await navigator.clipboard.writeText(message.content);
      setCopiedId(message.id);
      window.setTimeout(() => setCopiedId(null), 1800);
    } catch {
      setToast('无法访问剪贴板，请手动选择文字复制。');
    }
  }

  /** 使用该回答前面的用户问题重试，替换原回答而不重复追加消息。 */
  function retryMessage(messageId: string) {
    if (!active || pending || requestRef.current || active.generating) return;
    const index = active.messages.findIndex((message) => message.id === messageId);
    const question = active.messages[index - 1];
    if (index < 1 || question?.role !== 'user') return;
    updateAnswer(active.id, messageId, '', 'pending');
    shouldFollowRef.current = true;
    void requestAnswer(active.id, messageId, question.content);
  }

  /** 用户手动回到最新消息后，继续跟随新回答滚动。 */
  function scrollToBottom() {
    shouldFollowRef.current = true;
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
  }

  // 欢迎页和对话页复用同一个输入框，只改变它在布局中的位置。
  const composer = (
    <div className={`composer-dock ${isWelcome ? 'composer-welcome' : ''}`}>
      {!isWelcome && showScrollButton && (
        <button
          className="scroll-bottom icon-button"
          onClick={scrollToBottom}
          aria-label="回到最新消息"
          title="回到最新消息"
        >
          <ArrowDown size={17} />
        </button>
      )}
      <form className="composer" onSubmit={submitMessage}>
        <label className="sr-only" htmlFor="chat-input">
          输入你的问题
        </label>
        <textarea
          id="chat-input"
          ref={inputRef}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={handleInputKey}
          onCompositionStart={() => {
            composingRef.current = true;
          }}
          onCompositionEnd={() => {
            composingRef.current = false;
          }}
          placeholder="输入消息……"
          maxLength={20000}
          disabled={creating}
          title="Enter 发送，Shift + Enter 换行"
          rows={1}
        />
        <div className="composer-toolbar">
          <span
            className="model-caption"
            role="status"
            aria-label="当前模型"
            title={active ? `会话模型：${displayedModel}` : `新会话默认模型：${displayedModel}`}
          >
            <Cpu size={14} strokeWidth={1.5} aria-hidden="true" />
            <span>{displayedModel}</span>
          </span>
          {isActivePending ? (
            <button
              className="send-button stop-button"
              type="button"
              onClick={() => requestRef.current?.abort()}
              aria-label="停止等待回答"
              title="停止等待回答"
            >
              <Square size={13} fill="currentColor" />
            </button>
          ) : (
            <button
              className="send-button"
              type="submit"
              disabled={!draft.trim() || !user || !!pending || creating || historyLoading || !!historyError || !!active?.generating || (!!activeId && renaming?.id === activeId) || draft.trim().length > 20000}
              aria-label="发送消息"
              title={pending ? '另一段对话正在回答，请稍候' : '发送消息'}
            >
              <ArrowUp size={19} strokeWidth={2.2} />
            </button>
          )}
        </div>
      </form>
    </div>
  );

  return (
    <div
      className={`workspace ${sidebarOpen ? 'sidebar-visible' : 'sidebar-hidden'} ${resizingSidebar ? 'is-resizing' : ''}`}
      style={{ '--sidebar-width': `${visibleSidebarWidth}px` } as CSSProperties}
    >
      {sidebarOpen && (
        <button
          className="sidebar-backdrop"
          aria-label="关闭侧栏"
          onClick={() => setSidebarOpen(false)}
        />
      )}
      <aside
        id="conversation-sidebar"
        className="sidebar"
        aria-label="会话导航"
        inert={!sidebarOpen}
      >
        <div className="sidebar-brand">
          <button className="brand-button" onClick={newConversation} aria-label="CQ Assist 首页">
            <BrandMark />
            <span>
              CQ <span className="brand-light">Assist</span>
            </span>
          </button>
          <button
            className="icon-button sidebar-collapse"
            onClick={() => setSidebarOpen(false)}
            title="收起侧栏"
            aria-label="收起侧栏"
          >
            <PanelLeftClose size={18} />
          </button>
        </div>
        <div className="sidebar-actions">
          <button className="new-chat-button" onClick={newConversation}>
            <SquarePen size={17} />
            <span>新对话</span>
          </button>
          <button
            ref={searchTriggerRef}
            className="search-button"
            onClick={() => setSearchOpen(true)}
          >
            <Search size={16} />
            <span>搜索对话</span>
            <kbd>⌘ K</kbd>
          </button>
        </div>
        <div className="history-area">
          <div className="history-label">
            <span>最近对话</span>
          </div>
          {conversations.length === 0 ? (
            <div className="history-empty">
              <p>{!user ? '暂无对话' : historyLoading ? '正在加载…' : historyError ? '会话未加载' : '暂无对话'}</p>
            </div>
          ) : (
            ['今天', '昨天', '更早'].map((group) => {
              const items = [...conversations]
                .sort((first, second) => second.updatedAt - first.updatedAt)
                .filter((item) => conversationGroup(item.updatedAt) === group);
              return (
                items.length > 0 && (
                  <div className="history-group" key={group}>
                    <p>{group}</p>
                    {items.map((item) => (
                      <div
                        key={item.id}
                        className={`history-row ${item.id === activeId ? 'active' : ''}`}
                      >
                        {renaming?.id === item.id ? (
                          <form className="history-rename" onSubmit={saveTitle} aria-label="修改会话标题">
                            <div className="history-rename-controls">
                              <input autoFocus aria-label="会话标题" value={renaming.title}
                                disabled={savingTitle} required
                                onFocus={(event) => event.currentTarget.select()}
                                onChange={(event) => setRenaming({ ...renaming, title: event.target.value, error: '' })}
                                onKeyDown={(event) => {
                                  if (event.key === 'Escape' && !savingTitle) setRenaming(null);
                                  if (event.key === 'Enter' && event.nativeEvent.isComposing) event.preventDefault();
                                }} />
                              <button className="icon-button" type="submit" aria-label="保存标题"
                                title="保存标题" disabled={savingTitle || !renaming.title.trim()}>
                                {savingTitle ? <span className="small-spinner" /> : <Check size={14} />}
                              </button>
                              <button className="icon-button" type="button" aria-label="取消修改标题"
                                title="取消" disabled={savingTitle} onClick={() => setRenaming(null)}><X size={14} /></button>
                            </div>
                            {renaming.error && <p className="history-rename-error" role="alert">{renaming.error}</p>}
                          </form>
                        ) : <>
                        <button
                          className="history-item"
                          onClick={() => openConversation(item.id)}
                          aria-current={item.id === activeId ? 'page' : undefined}
                        >
                          <MessageSquare size={14} />
                          <span title={item.title}>{item.title}</span>
                          {pending?.conversationId === item.id && (
                            <span className="small-spinner" />
                          )}
                        </button>
                        <button className="history-action icon-button" onClick={() => startRename(item)}
                          aria-label={`修改标题：${item.title}`} title="修改标题"
                          disabled={savingTitle || item.generating || pending?.conversationId === item.id}>
                          <Pencil size={14} />
                        </button>
                        <button
                          className="history-action icon-button"
                          onClick={() => deleteConversation(item.id)}
                          aria-label={`删除对话：${item.title}`}
                          title="删除对话"
                        >
                          <Trash2 size={14} />
                        </button>
                        </>}
                      </div>
                    ))}
                  </div>
                )
              );
            })
          )}
        </div>
        <div className="sidebar-footer">
          <div className="profile-row">
            <span className="user-avatar" aria-hidden="true">{user ? Array.from(user.display_name)[0]?.toUpperCase() : <UserRound size={15} />}</span>
            <div className="user-details" title={user?.display_name}>
              <strong>{user?.display_name ?? (profileLoading ? '读取资料…' : '资料未加载')}</strong><small>个人空间</small>
            </div>
            <button className="icon-button" title="编辑个人资料" aria-label="编辑个人资料" onClick={onEditProfile} disabled={!user}>
              <UserRound size={16} />
            </button>
            <button
              className="icon-button theme-button"
              onClick={() => setTheme(theme === 'light' ? 'dark' : 'light')}
              title={theme === 'light' ? '切换深色主题' : '切换浅色主题'}
              aria-label={theme === 'light' ? '切换深色主题' : '切换浅色主题'}
            >
              {theme === 'light' ? <Moon size={17} /> : <Sun size={17} />}
            </button>
          </div>
        </div>
        <div
          className="sidebar-resizer"
          role="separator"
          aria-label="调整侧栏宽度"
          aria-orientation="vertical"
          aria-controls="conversation-sidebar"
          aria-valuemin={SIDEBAR_MIN_WIDTH}
          aria-valuemax={maxSidebarWidth}
          aria-valuenow={visibleSidebarWidth}
          aria-valuetext={`${visibleSidebarWidth} 像素`}
          tabIndex={sidebarOpen && viewportWidth > 760 ? 0 : -1}
          title="拖动调整宽度，双击还原；方向键微调"
          onPointerDown={startSidebarResize}
          onPointerMove={moveSidebarResize}
          onPointerUp={finishSidebarResize}
          onPointerCancel={finishSidebarResize}
          onLostPointerCapture={finishSidebarResize}
          onDoubleClick={() => setSidebarWidth(SIDEBAR_DEFAULT_WIDTH)}
          onKeyDown={handleSidebarResizeKey}
        />
      </aside>

      <main className={`main-panel ${isWelcome ? 'welcome-mode' : 'conversation-mode'}`}>
        <header className="topbar">
          <div className="topbar-left">
            {!sidebarOpen && (
              <button
                className="icon-button"
                onClick={() => setSidebarOpen(true)}
                aria-label="展开侧栏"
                title="展开侧栏"
              >
                <PanelLeftOpen size={19} />
              </button>
            )}
            <span className="topbar-title">{active?.title ?? '新对话'}</span>
          </div>
        </header>

        {profileError && <div className="workspace-error" role="alert">{profileError}<button className="text-button" onClick={onRetryProfile}>重新连接</button></div>}
        {!profileError && historyError && <div className="workspace-error" role="alert">{historyError}<button className="text-button" onClick={() => void reloadConversations()}>重试</button></div>}
        {active?.generating && !isActivePending && <div className="workspace-error" role="status">后台正在回答，完成后会自动恢复。</div>}
        <div
          className="conversation-scroll"
          ref={scrollRef}
          onScroll={() => {
            const element = scrollRef.current;
            if (!element) return;
            const nearBottom =
              element.scrollHeight - element.scrollTop - element.clientHeight < 100;
            shouldFollowRef.current = nearBottom;
            setShowScrollButton(!nearBottom);
          }}
        >
          {isWelcome ? (
            <section className="welcome-hero" aria-label="新对话">
              <div className="welcome-intro">
                <h1>
                  你好，我是 <span>CQ Assist</span>
                </h1>
                <p>你的个人 AI 助手，从一个问题开始。</p>
              </div>
              {composer}
            </section>
          ) : (
            <div className="messages" role="log" aria-label="对话消息" aria-busy={isActivePending}>
              {messages.map((message) => (
                <article
                  key={message.id}
                  className={`message message-${message.role} ${message.status ? `message-${message.status}` : ''}`}
                >
                  {message.role === 'user' ? (
                    <>
                      <div className="user-message">{message.content}</div>
                    </>
                  ) : (
                    <>
                      <div className="assistant-identity">
                        <BrandMark />
                        <span>CQ Assist</span>
                      </div>
                      {message.status === 'pending' ? (
                        <div className="thinking-content" role="status" aria-label="正在回答">
                          <span className="thinking-dots">
                            <i />
                            <i />
                            <i />
                          </span>
                        </div>
                      ) : message.status === 'error' || message.status === 'cancelled' ? (
                        <div className="answer-notice">
                          <p>{message.content}</p>
                          <button
                            className="text-button"
                            onClick={() => retryMessage(message.id)}
                            disabled={!!pending || !!active?.generating}
                          >
                            <RotateCcw size={13} />
                            {message.status === 'error' ? '重试' : '重新回答'}
                          </button>
                        </div>
                      ) : (
                        <>
                          <Suspense
                            fallback={
                              <div className="markdown markdown-loading">{message.content}</div>
                            }
                          >
                            <Markdown content={message.content} />
                          </Suspense>
                          <div className="message-actions">
                            <button
                              className="icon-button"
                              aria-label={copiedId === message.id ? '回答已复制' : '复制回答'}
                              title={copiedId === message.id ? '已复制' : '复制回答'}
                              onClick={() => void copyMessage(message)}
                            >
                              {copiedId === message.id ? <Check size={15} /> : <Copy size={15} />}
                            </button>

                          </div>
                        </>
                      )}
                    </>
                  )}
                </article>
              ))}
            </div>
          )}
        </div>

        {!isWelcome && composer}
      </main>

      <dialog
        ref={searchDialogRef}
        className="search-dialog"
        onCancel={() => setSearchOpen(false)}
        onClick={(event) => {
          if (event.target === event.currentTarget) setSearchOpen(false);
        }}
        aria-label="搜索对话"
      >
        <div className="search-dialog-input">
          <Search size={19} />
          <input
            ref={searchRef}
            aria-label="搜索关键词"
            placeholder="搜索标题或对话内容……"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
          <button
            className="icon-button"
            aria-label="关闭搜索"
            onClick={() => setSearchOpen(false)}
          >
            <X size={17} />
          </button>
        </div>
        <div className="search-results">
          {filteredConversations.length > 0 ? (
            filteredConversations.map((item) => (
              <button key={item.id} onClick={() => openConversation(item.id)}>
                <MessageSquare size={17} />
                <span>
                  <strong>{item.title}</strong>
                  <small>
                    {conversationGroup(item.updatedAt)} · {Math.floor(item.messages.length / 2)}{' '}
                    次问答
                  </small>
                </span>
                <ArrowUpRight size={15} />
              </button>
            ))
          ) : (
            <div className="search-no-results">
              <Search size={24} strokeWidth={1.4} />
              <p>{search ? '没有找到相关对话' : '还没有对话记录'}</p>
            </div>
          )}
        </div>
        <div className="search-dialog-footer">
          <span>个人会话记录</span>
          <kbd>ESC 关闭</kbd>
        </div>
      </dialog>
      {toast && (
        <div className="toast" role="status">
          <Check size={15} />
          {toast}
        </div>
      )}
    </div>
  );
}
