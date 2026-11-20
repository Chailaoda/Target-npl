import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, CheckCircle2, GitCompare, RefreshCw, ShieldQuestion } from 'lucide-react'
import Card from '../components/Card'
import type { Api } from '../lib/apiClient'
import type { Conflict, ReviewItem } from '../types'

export const isEditableTarget = (target: EventTarget | null): boolean => {
  const element = target as HTMLElement | null
  if (!element || !element.tagName) return false
  const tag = element.tagName.toLowerCase()
  return tag === 'input' || tag === 'textarea' || tag === 'select' || element.isContentEditable === true
}

export const nextIndex = (current: number, delta: number, length: number): number => {
  if (length <= 0) return 0
  const candidate = current + delta
  if (candidate < 0) return 0
  if (candidate > length - 1) return length - 1
  return candidate
}

type Props = { api: Api; notices: { notify: (t: string) => void; fail: (t: string) => void } }

/** Two-pane human review: attribution conflicts on the left, queue on the right. */
export default function ReviewWorkbenchPage({ api, notices }: Props) {
  const [reviews, setReviews] = useState<ReviewItem[]>([])
  const [conflicts, setConflicts] = useState<Conflict[]>([])
  const [rIndex, setRIndex] = useState(0)
  const [cIndex, setCIndex] = useState(0)
  const [pane, setPane] = useState<'review' | 'conflict'>('review')
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      const [queue, conflictList] = await Promise.all([
        api<{ items: ReviewItem[] }>('/api/reviews/pending?limit=50'),
        api<{ items: Conflict[] }>('/api/accounts/conflicts?status=pending&limit=50')
      ])
      setReviews(queue.items || [])
      setConflicts(conflictList.items || [])
      setRIndex(0)
      setCIndex(0)
    } catch (error) {
      notices.fail((error as Error).message)
    }
  }, [api])

  useEffect(() => { void load() }, [load])

  const submitReview = async (decision: 'accept' | 'reject') => {
    const item = reviews[rIndex]
    if (!item || busy) return
    const previous = reviews
    const remaining = reviews.filter(entry => entry.analysis_key !== item.analysis_key)
    setReviews(remaining)
    setRIndex(current => nextIndex(current, 0, remaining.length))
    setBusy(true)
    try {
      await api('/api/reviews', { items: [{ analysis_key: item.analysis_key, decision }] })
    } catch (error) {
      setReviews(previous)
      notices.fail(`${(error as Error).message}（已回滚该条，请刷新后重试）`)
      await load()
    } finally {
      setBusy(false)
    }
  }

  const resolveConflict = async (chosen: 'current' | 'candidate') => {
    const item = conflicts[cIndex]
    if (!item || busy) return
    const chosenKey = chosen === 'current' ? item.current_key : item.candidate_key
    const previous = conflicts
    const remaining = conflicts.filter(entry => entry.conflict_id !== item.conflict_id)
    setConflicts(remaining)
    setCIndex(current => nextIndex(current, 0, remaining.length))
    setBusy(true)
    try {
      await api('/api/accounts/conflicts/resolve', {
        platform: item.platform,
        object_type: item.object_type,
        content_id: item.content_id,
        chosen_key: chosenKey,
        note: '复核工作台人工裁决'
      })
      notices.notify(`已确认归属：${chosenKey}`)
    } catch (error) {
      setConflicts(previous)
      notices.fail(`${(error as Error).message}（已回滚裁决）`)
      await load()
    } finally {
      setBusy(false)
    }
  }

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (isEditableTarget(event.target) || event.metaKey || event.ctrlKey || event.altKey) return
      const key = event.key.toLowerCase()
      if (key === 'f') {
        event.preventDefault()
        setPane(current => (current === 'review' ? 'conflict' : 'review'))
        return
      }
      if (key === 'j' || key === 'k') {
        event.preventDefault()
        const delta = key === 'j' ? 1 : -1
        if (pane === 'review') setRIndex(current => nextIndex(current, delta, reviews.length))
        else setCIndex(current => nextIndex(current, delta, conflicts.length))
        return
      }
      if (pane === 'review') {
        if (key === 'a') { event.preventDefault(); void submitReview('accept') }
        if (key === 'r') { event.preventDefault(); void submitReview('reject') }
        return
      }
      if (key === '1' || key === 'a') { event.preventDefault(); void resolveConflict('current') }
      if (key === '2' || key === 'r') { event.preventDefault(); void resolveConflict('candidate') }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [pane, reviews, conflicts, rIndex, cIndex, busy])

  const activeReview = reviews[rIndex]
  const activeConflict = conflicts[cIndex]

  return (
    <div className="review-page">
      <div className="review-bar">
        <span className={`review-tab ${pane === 'review' ? 'on' : ''}`} onClick={() => setPane('review')} role="button" tabIndex={0}>
          复核队列 {reviews.length}
        </span>
        <span className={`review-tab ${pane === 'conflict' ? 'on' : ''}`} onClick={() => setPane('conflict')} role="button" tabIndex={0}>
          归属冲突 {conflicts.length}
        </span>
        <small>J/K 切换条目 · A 接受 · R 驳回 · 冲突面板 1 保留当前 / 2 改判候选 · F 切换面板</small>
        <button type="button" className="btn btn-sm" onClick={() => void load()}><RefreshCw size={12} />刷新</button>
      </div>

      <div className="review-grid">
        <Card title="待复核内容" desc="模型判定不确定或需要人看的条目；接受后进入候选结果。" aside={<ShieldQuestion size={15} />}>
          {reviews.length === 0 ? (
            <div className="empty">队列已清空</div>
          ) : (
            <ul className="review-list">
              {reviews.map((item, index) => (
                <li key={item.analysis_key} className={index === rIndex && pane === 'review' ? 'active' : ''} onClick={() => { setPane('review'); setRIndex(index) }}>
                  <div className="review-meta">
                    <b>{item.platform || '未知平台'}</b>
                    <span>{item.stance} · {item.intent} · 分 {item.score ?? '—'}</span>
                  </div>
                  <p>{item.text}</p>
                  {item.reason && <small>{item.reason}</small>}
                </li>
              ))}
            </ul>
          )}
          {activeReview && (
            <div className="review-actions">
              <button type="button" className="btn btn-sm" onClick={() => void submitReview('accept')} disabled={busy}><CheckCircle2 size={12} />接受 (A)</button>
              <button type="button" className="btn btn-sm btn-danger" onClick={() => void submitReview('reject')} disabled={busy}><AlertTriangle size={12} />驳回 (R)</button>
            </div>
          )}
        </Card>

        <Card title="归属冲突" desc="同一条内容被两个账号认领时，由人工确认唯一归属。" aside={<GitCompare size={15} />}>
          {conflicts.length === 0 ? (
            <div className="empty">没有待裁决冲突</div>
          ) : (
            <ul className="conflict-list">
              {conflicts.map((item, index) => (
                <li key={item.conflict_id} className={index === cIndex && pane === 'conflict' ? 'active' : ''} onClick={() => { setPane('conflict'); setCIndex(index) }}>
                  <p>{item.snippet || '（无原文片段，请到来源核对）'}</p>
                  <div className="conflict-pair">
                    <span className={index === cIndex ? 'pick' : ''}>当前 {item.current_key} · {item.current_authority || '—'}</span>
                    <span>候选 {item.candidate_key} · {item.candidate_authority || '—'}</span>
                  </div>
                </li>
              ))}
            </ul>
          )}
          {activeConflict && (
            <div className="review-actions">
              <button type="button" className="btn btn-sm" onClick={() => void resolveConflict('current')} disabled={busy}>保留当前 (1)</button>
              <button type="button" className="btn btn-sm" onClick={() => void resolveConflict('candidate')} disabled={busy}>改判候选 (2)</button>
            </div>
          )}
        </Card>
      </div>
    </div>
  )
}
