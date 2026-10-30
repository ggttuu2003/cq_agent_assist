import { useEffect, useRef, useState, type FormEvent } from 'react';
import ChatWorkspace from './App';
import { getProfile, updateProfile, type User } from './api';

/** 资料表单只用于修改昵称，首次进入不要求用户填写。 */
function ProfileForm({ user, onSaved, onCancel }: {
  user: User; onSaved: (user: User) => void; onCancel: () => void;
}) {
  const [name, setName] = useState(user.display_name);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || !name.trim()) return;
    setBusy(true);
    setError('');
    try { onSaved(await updateProfile(name.trim())); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '保存失败，请重试。'); }
    finally { setBusy(false); }
  }

  return <section className="profile-card" aria-label="个人资料">
    <div className="profile-brand">CQ <span>Assist</span></div>
    <h1>个人资料</h1>
    <p>修改侧栏显示的昵称。</p>
    <form onSubmit={submit}>
      <label>昵称<input value={name} onChange={(event) => setName(event.target.value)} autoFocus required maxLength={40} autoComplete="nickname" /></label>
      {error && <div className="profile-error" role="alert">{error}</div>}
      <button className="profile-submit" disabled={busy || !name.trim()}>{busy ? '保存中…' : '保存'}</button>
      <button type="button" className="profile-cancel" onClick={onCancel}>取消</button>
    </form>
  </section>;
}

/** 原生弹窗负责焦点限制和 Escape 关闭，编辑昵称时不离开聊天页面。 */
function ProfileDialog({ user, onSaved, onClose }: { user: User; onSaved: (user: User) => void; onClose: () => void }) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  useEffect(() => { dialogRef.current?.showModal(); }, []);
  return <dialog ref={dialogRef} className="profile-dialog" aria-label="编辑个人资料" onCancel={onClose} onClick={(event) => {
    if (event.target === event.currentTarget) onClose();
  }}><ProfileForm user={user} onSaved={onSaved} onCancel={onClose} /></dialog>;
}

/** 始终显示聊天主界面，个人资料在后台读取，失败时在主界面重试。 */
export default function Profile() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [editing, setEditing] = useState(false);
  const [connectionAttempt, setConnectionAttempt] = useState(0);
  const profileRequestRef = useRef<AbortController | null>(null);

  /** 同一时间只读取一次资料，首次加载和手动重连共用取消与状态处理。 */
  async function initialize() {
    if (profileRequestRef.current) return;
    const controller = new AbortController();
    profileRequestRef.current = controller;
    const signal = controller.signal;
    setLoading(true);
    setError('');
    try {
      const saved = await getProfile(signal);
      if (!signal.aborted) setUser(saved);
    } catch {
      if (!signal.aborted) setError('暂时无法连接服务');
    } finally {
      if (!signal.aborted) setLoading(false);
      if (profileRequestRef.current === controller) profileRequestRef.current = null;
    }
  }

  /** 只有用户主动重连时，才让模型信息也重新读取，资料更新不会触发它。 */
  function retryConnection() {
    if (profileRequestRef.current) return;
    setConnectionAttempt((attempt) => attempt + 1);
    void initialize();
  }

  useEffect(() => {
    // 下一轮事件循环再发送：StrictMode 首次检查会立即清理，先撤销尚未发出的请求。
    const timer = window.setTimeout(() => void initialize(), 0);
    return () => {
      window.clearTimeout(timer);
      // 真正离开页面时，首次加载和手动重连中尚未完成的请求都要取消。
      profileRequestRef.current?.abort();
      profileRequestRef.current = null;
    };
  }, []);

  return <>
    <ChatWorkspace user={user} profileLoading={loading} profileError={error} connectionAttempt={connectionAttempt}
      onRetryProfile={retryConnection} onEditProfile={() => setEditing(true)} />
    {editing && user && <ProfileDialog user={user} onSaved={(saved) => { setUser(saved); setEditing(false); }} onClose={() => setEditing(false)} />}
  </>;
}
