/** Shared view-model types for the control workbench. */
export type Policy = {
  schema_version: number
  id: string
  name: string
  topic_patterns: string[]
  ambiguous_reply_patterns: string[]
  dimensions: Record<string, string[]>
  rules: Record<string, string[]>
  decision_gates?: Record<string, string[]>
  acceptance: { stances: string[]; intents: string[] }
  display: { sort_weights: Record<string, number>; include_related: boolean }
}

export type Collection = {
  schema_version: number
  platform: string
  platforms?: string[]
  keywords: string[]
  pages_per_keyword: number
  posts_per_keyword: number
  max_posts: number
  comments_per_post: number
  request_interval_seconds: number
  max_requests: number
  timeout_seconds: number
  proxy_mode: string
}

export type Task = {
  schema_version: number
  description: string
  scope: {
    locations: string[]
    recent_days: number | null
    published_after: string | null
    published_before: string | null
  }
  exclusions: {
    promotions: boolean
    terms: string[]
  }
  execution: {
    source: 'existing' | 'collect'
    dataset_id: string
    mode: 'rules' | 'ark'
    max_api_calls: number
  }
  assumptions: string[]
  unresolved: string[]
  human_overrides: string[]
}

export type Bundle = {
  policy: Policy
  collection: Collection
  task?: Task
}

export type State = {
  bundle: Bundle
  revision: string
  etag: string
  token?: string
}

export type Version = {
  revision: string
  created_at: string
  name: string
}

export type RunState = {
  run_id: string
  status: string
  phase?: string
  message?: string
  collection_incomplete?: boolean
  result?: { candidate_contents: number; unique_contents: number; report: string }
}

export type PreviewResult = {
  accepted: boolean
  score: number
  evidence_level: string
  quality_flags: string[]
  topic: string
  stance: string
  intent: string
  promotion: string
  author_role: string
  reason: string
  scope_check?: {
    status: string
    reasons: string[]
  }
  evidence?: Record<string, any>
}

export type View = 'workbench' | 'monitor' | 'discover' | 'review' | 'history'

export type Conflict = {
  conflict_id: number
  platform: string
  object_type: string
  content_id: string
  current_key: string
  current_authority: string
  candidate_key: string
  candidate_authority: string
  snippet: string
  detected_at: string
  status: string
  resolution: string
}

export type ReviewItem = {
  analysis_key: string
  platform?: string
  text?: string
  status?: string
  score?: number
  reason?: string
  stance?: string
  intent?: string
  evidence?: { quote?: string } | Record<string, any>
}

export const makeTask = (bundle: Bundle): Task => ({
  schema_version: 1,
  description: bundle.policy.name,
  scope: { locations: [], recent_days: null, published_after: null, published_before: null },
  exclusions: { promotions: true, terms: [] },
  execution: { source: 'collect', dataset_id: 'latest', mode: 'ark', max_api_calls: 100 },
  assumptions: [],
  unresolved: [],
  human_overrides: []
})

export const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T
