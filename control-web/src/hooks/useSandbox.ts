import { useState } from 'react'
import type { Api } from '../lib/apiClient'
import type { Bundle, PreviewResult } from '../types'

export type Sandbox = {
  contextText: string
  commentText: string
  result: PreviewResult | null
  busy: boolean
  setContextText: (value: string) => void
  setCommentText: (value: string) => void
  run: (bundle: Bundle, text?: string, context?: string) => Promise<void>
}

/** One-shot rule preview. Kept outside the polling hooks: it is user-driven. */
export function useSandbox(api: Api, fail: (text: string) => void): Sandbox {
  const [contextText, setContextText] = useState('')
  const [commentText, setCommentText] = useState('太酷了，有人一起吗？求搭子交流！')
  const [result, setResult] = useState<PreviewResult | null>(null)
  const [busy, setBusy] = useState(false)

  async function run(bundle: Bundle, text?: string, context?: string) {
    const target = text ?? commentText
    if (!target.trim()) return
    setBusy(true)
    try {
      setResult(await api<PreviewResult>('/api/preview', {
        policy: bundle.policy,
        text: target,
        context_text: (context ?? contextText).trim() || undefined,
        task: bundle.task
      }))
    } catch (error) {
      fail((error as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return { contextText, commentText, result, busy, setContextText, setCommentText, run }
}
