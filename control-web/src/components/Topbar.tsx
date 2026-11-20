import { Loader2, Play, Save } from 'lucide-react'

type Props = {
  title: [string, string]
  dirty: boolean
  busy: boolean
  running: boolean
  onSave: () => void
  onStart: () => void
}

export default function Topbar({ title, dirty, busy, running, onSave, onStart }: Props) {
  return (
    <header className="topbar">
      <div className="page-title">
        <h1>{title[0]}</h1>
        <p>{title[1]}</p>
      </div>
      <div className="top-actions">
        {dirty && <span className="dirty"><i />有未保存的修改</span>}
        <button type="button" className="btn" onClick={onSave} disabled={busy || !dirty}>
          <Save size={14} />保存草稿
        </button>
        <button type="button" className="btn btn-primary" onClick={onStart} disabled={busy || running}>
          {busy ? <Loader2 size={14} className="spin" /> : <Play size={14} fill="currentColor" />}
          {running ? '运行中' : '开始抓取'}
        </button>
      </div>
    </header>
  )
}
