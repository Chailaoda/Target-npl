import { useEffect, useRef, useState } from 'react'
import type { LiveData } from '../RunMonitor'
import type { RunState } from '../types'
import type { Api } from '../lib/apiClient'

type LiveDelta = LiveData & {
  incremental?: boolean
  next_seq?: number
  captured_delta?: LiveData['captured']
  candidates_delta?: LiveData['candidates']
}

const keyed = <T extends { id?: string }>(items: T[]) =>
  Array.from(new Map(items.map(item => [item.id, item])).values())

function mergeLive(previous: LiveData | null, incoming: LiveDelta): LiveData {
  if (!incoming.incremental || typeof incoming.next_seq !== 'number') return incoming
  const base = previous && previous.run_id === incoming.run_id ? previous : null
  return {
    ...incoming,
    captured: keyed([...(base?.captured || []), ...(incoming.captured_delta || [])]),
    candidates: keyed([...(base?.candidates || []), ...(incoming.candidates_delta || [])])
  }
}

/**
 * The single owner of run status and live data.
 *
 * Only this hook polls. Pages read the snapshot it publishes, so a 2s poll can
 * never re-render the rule form the operator is typing into. When the tab is
 * hidden the interval backs off to 6s and the cursor keeps the payload small.
 */
export function useRunStream(api: Api, enabled: boolean) {
  const [runState, setRunState] = useState<RunState | null>(null)
  const [liveData, setLiveData] = useState<LiveData | null>(null)
  const [liveError, setLiveError] = useState('')
  const cursor = useRef(0)
  const liveRunId = useRef<string | null>(null)

  useEffect(() => {
    if (!enabled) return
    let cancelled = false
    api<RunState | null>('/api/tasks/latest')
      .then(value => { if (!cancelled && value) setRunState(value) })
      .catch(() => {})
    return () => { cancelled = true }
  }, [api, enabled])

  useEffect(() => {
    if (!runState) return
    const runId = runState.run_id
    if (liveRunId.current !== runId) {
      liveRunId.current = runId
      cursor.current = 0
      setLiveData(null)
    }
    const running = ['queued', 'running'].includes(runState.status)
    let cancelled = false
    let pending = false
    let timer: ReturnType<typeof setTimeout> | null = null

    const tick = async () => {
      if (pending || cancelled) return
      pending = true
      try {
        const body: Record<string, unknown> = { run_id: runId }
        if (cursor.current) body.since_seq = cursor.current
        const [status, live] = await Promise.all([
          running ? api<RunState>('/api/tasks/status', { run_id: runId }) : Promise.resolve(null),
          api<LiveDelta>('/api/tasks/live', body)
        ])
        if (cancelled) return
        if (live) {
          if (typeof live.next_seq === 'number') cursor.current = live.next_seq
          setLiveData(previous => mergeLive(previous, live))
        }
        if (status) setRunState(status)
        setLiveError('')
      } catch (error) {
        if (!cancelled) setLiveError((error as Error).message)
      } finally {
        pending = false
      }
    }

    const schedule = () => {
      // visibilityState, not document.hidden: hidden is true in non-visual
      // DOM environments (tests) where the page is actually being observed.
      const delay = document.visibilityState === 'hidden' ? 6000 : 2000
      timer = setTimeout(() => {
        void tick().then(() => { if (!cancelled) schedule() })
      }, delay)
    }

    void tick().then(() => { if (!cancelled) schedule() })
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [api, runState?.run_id, runState?.status])

  const running = !!runState && ['queued', 'running'].includes(runState.status)
  return { runState, setRunState, liveData, liveError, running }
}
