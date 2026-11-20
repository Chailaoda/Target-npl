import { useEffect, useRef, useState } from 'react'
import type { Api } from '../lib/apiClient'
import { makeTask, clone } from '../types'
import type { Bundle, RunState, State, Version } from '../types'
import {
  updatePatterns,
  mergeSavedEdits,
  patternsToTags,
  tagsToPatterns,
  syncSimpleWordsToPolicy,
  type PresetTemplate
} from '../presets'

export type Notices = {
  notify: (text: string) => void
  fail: (text: string) => void
  clear: () => void
}

/**
 * Owns everything the operator is editing: the active snapshot, the draft,
 * the history list and the goal text. Polling never touches this state, so
 * typing cannot be interrupted by a background refresh.
 */
export function useConfigDraft(
  api: Api,
  notices: Notices,
  onToken: (token: string) => void,
  onStarted: (run: RunState) => void
) {
  const [active, setActive] = useState<State | null>(null)
  const [draft, setDraft] = useState<Bundle | null>(null)
  const [versions, setVersions] = useState<Version[]>([])
  const [busy, setBusy] = useState(false)
  const [goalText, setGoalText] = useState('')
  const [jsonText, setJsonText] = useState('')

  const draftRef = useRef<Bundle | null>(null)
  const editSequence = useRef(0)
  const planRef = useRef<string | null>(null)

  function loadDraft(bundle: Bundle) {
    const next = clone(bundle)
    next.task = next.task || makeTask(next)
    draftRef.current = next
    setDraft(next)
    setGoalText(next.task.description || '')
    setJsonText(JSON.stringify(next, null, 2))
    editSequence.current++
    notices.clear()
  }

  function updateDraft(mutator: (bundle: Bundle) => void) {
    if (!draftRef.current) return
    const next = clone(draftRef.current)
    mutator(next)
    draftRef.current = next
    setDraft(next)
    setJsonText(JSON.stringify(next, null, 2))
    editSequence.current++
    notices.clear()
  }

  useEffect(() => {
    let disposed = false
    Promise.all([api<State>('/api/state'), api<Version[]>('/api/versions')])
      .then(([state, history]) => {
        if (disposed) return
        onToken(state.token || '')
        setActive(state)
        loadDraft(state.bundle)
        setVersions(history)
      })
      .catch(error => { if (!disposed) notices.fail(String((error as Error).message)) })
    return () => { disposed = true }
  }, [])

  const dirty = !!draft && !!active && JSON.stringify(draft) !== JSON.stringify(active.bundle)

  const topicWords = patternsToTags(draft?.policy.topic_patterns || [])
  const exactSignalWords = patternsToTags(draft?.policy.rules?.intent_seeking_company || [])
  const praiseWords = patternsToTags(draft?.policy.rules?.stance_positive || [])
  const excludeWords = Array.from(new Set([
    ...(draft?.task?.exclusions.terms || []),
    ...patternsToTags(draft?.policy.rules?.promotion_yes || [])
  ]))

  const handleUpdateWords = (type: 'topic' | 'exact' | 'praise' | 'exclude', newTags: string[]) => {
    updateDraft(b => {
      b.policy.rules = b.policy.rules || {}
      b.policy.decision_gates = b.policy.decision_gates || {}
      if (type === 'topic') {
        b.policy.topic_patterns = newTags.length > 0 ? updatePatterns(b.policy.topic_patterns, newTags) : ['搭子']
      } else if (type === 'exact') {
        b.policy.decision_gates.intent_seeking_company = updatePatterns(b.policy.decision_gates.intent_seeking_company || b.policy.rules.intent_seeking_company || [], newTags)
        b.policy.rules.intent_seeking_company = updatePatterns(b.policy.rules.intent_seeking_company || [], newTags)
      } else if (type === 'praise') {
        b.policy.decision_gates.stance_positive = updatePatterns(b.policy.decision_gates.stance_positive || b.policy.rules.stance_positive || [], newTags)
        b.policy.rules.stance_positive = updatePatterns(b.policy.rules.stance_positive || [], newTags)
      } else if (type === 'exclude') {
        b.policy.decision_gates.promotion_yes = updatePatterns(b.policy.rules.promotion_yes || [], newTags)
        b.policy.rules.promotion_yes = updatePatterns(b.policy.rules.promotion_yes || [], newTags)
        if (b.task && b.task.exclusions) {
          b.task.exclusions.terms = Array.from(new Set(newTags.filter(w => !/^已有复杂规则 \d+$/.test(w)).map(w => w.trim()).filter(Boolean)))
          b.task.exclusions.promotions = true
        }
      }
    })
  }

  const applyPreset = (preset: PresetTemplate) => {
    updateDraft(b => {
      b.policy.name = preset.name.replace(/^[\p{Emoji}\s]+/u, '')
      b.collection.keywords = clone(preset.keywords)
      if (b.task) {
        b.task.description = preset.description
        b.task.human_overrides = []
      }
      syncSimpleWordsToPolicy(
        b.policy,
        { topicWords: preset.topicWords, exactSignalWords: preset.exactSignalWords, praiseWords: preset.praiseWords, excludeWords: preset.excludeWords },
        b.task
      )
    })
    setGoalText(preset.description)
    notices.notify(`已载入「${preset.name}」词库，人工锁定已清空`)
  }

  const refreshVersions = async () => {
    try { setVersions(await api<Version[]>('/api/versions')) } catch {}
  }

  // Saving is a round trip: edits typed while it is in flight must survive.
  async function persistDraft(): Promise<State> {
    const seq = editSequence.current
    const snapshot = clone(draft!)
    const saved = await api<State>(planRef.current ? '/api/tasks/save-draft' : '/api/save', {
      bundle: snapshot,
      etag: active!.etag,
      ...(planRef.current ? { plan_id: planRef.current } : {})
    })
    planRef.current = null
    setActive({ ...saved, token: active!.token })
    if (editSequence.current === seq) {
      loadDraft(saved.bundle)
    } else if (draftRef.current) {
      const kept = mergeSavedEdits(draftRef.current, saved.bundle, snapshot)
      draftRef.current = kept
      setDraft(kept)
      setJsonText(JSON.stringify(kept, null, 2))
    }
    return saved
  }

  async function saveDraft() {
    if (!draft || !active) return
    setBusy(true)
    notices.clear()
    try {
      await persistDraft()
      notices.notify('已保存，下次抓取将使用这一版本')
      await refreshVersions()
    } catch (error) {
      notices.fail((error as Error).message)
    } finally {
      setBusy(false)
    }
  }

  async function saveAndStart() {
    if (!draft || !active) return
    setBusy(true)
    notices.clear()
    try {
      const saved = await persistDraft()
      const run = await api<RunState>('/api/tasks/run', { revision: saved.revision, request_id: crypto.randomUUID() })
      onStarted(run)
      notices.notify('已开始抓取，浏览器在后台运行；只有需要登录或验证时才会弹出')
      await refreshVersions()
    } catch (error) {
      notices.fail((error as Error).message)
    } finally {
      setBusy(false)
    }
  }

  async function handleAIGenerate() {
    if (!active || !goalText.trim()) return
    setBusy(true)
    notices.clear()
    try {
      let currentEtag = active.etag
      if (dirty && draft) {
        const saved = await api<State>('/api/save', { bundle: clone(draft), etag: active.etag })
        setActive({ ...saved, token: active.token })
        currentEtag = saved.etag
      }
      const proposal = await api<{ plan_id: string; bundle: Bundle; can_apply: boolean }>('/api/tasks/plan', { goal: goalText, base_etag: currentEtag })
      loadDraft(proposal.bundle)
      planRef.current = proposal.plan_id
      notices.notify('AI 已生成推荐词库与参数，请核对后保存')
    } catch (error) {
      notices.fail((error as Error).message)
    } finally {
      setBusy(false)
    }
  }

  async function importFile(file: File) {
    setBusy(true)
    notices.clear()
    try {
      const parsed = JSON.parse(await file.text())
      const bundle: Bundle = parsed.schema_version === 2
        ? { ...clone(draftRef.current!), policy: parsed }
        : parsed.bundle || (parsed.policy && parsed.collection ? parsed : null)
      if (!bundle) throw Error('无效的规则 JSON 文件')
      await api('/api/validate', { bundle })
      loadDraft(bundle)
      notices.notify(`已导入 ${file.name}，检查后保存即可生效`)
    } catch (error) {
      notices.fail((error as Error).message)
    } finally {
      setBusy(false)
    }
  }

  function exportFile() {
    if (!draft) return
    const url = URL.createObjectURL(new Blob([JSON.stringify(draft, null, 2)], { type: 'application/json' }))
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `${draft.policy.id || 'target-npl'}-rules.json`
    anchor.click()
    URL.revokeObjectURL(url)
  }

  function addBlacklistWords(input: string) {
    if (!input || !draftRef.current) return
    const items = input.split(/[\s,，、|]+/).map(word => word.trim()).filter(Boolean)
    if (items.length === 0) return
    updateDraft(b => {
      const merged = Array.from(new Set([...(b.task?.exclusions.terms || []), ...items]))
      if (b.task?.exclusions) {
        b.task.exclusions.terms = merged
        b.task.exclusions.promotions = true
      }
      const patterns = tagsToPatterns(merged)
      b.policy.rules = b.policy.rules || {}
      b.policy.decision_gates = b.policy.decision_gates || {}
      b.policy.rules.promotion_yes = Array.from(new Set([...(b.policy.rules.promotion_yes || []), ...patterns]))
      b.policy.decision_gates.promotion_yes = Array.from(new Set([...(b.policy.decision_gates.promotion_yes || []), ...patterns]))
    })
    notices.notify(`已将「${items.join('、')}」加入黑名单，保存后生效`)
  }

  return {
    active, draft, versions, busy, goalText, jsonText, dirty,
    topicWords, exactSignalWords, praiseWords, excludeWords,
    setGoalText, setJsonText, setDraft, setVersions,
    loadDraft, updateDraft, handleUpdateWords, applyPreset,
    saveDraft, saveAndStart, handleAIGenerate, importFile, exportFile, addBlacklistWords
  }
}
