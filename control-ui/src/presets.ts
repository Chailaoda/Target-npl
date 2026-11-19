/**
 * 纯文字傻瓜式标签与底层正则引擎双向转换器
 */

export interface PresetTemplate {
  id: string
  name: string
  icon: string
  description: string
  keywords: string[]
  topicWords: string[]
  exactSignalWords: string[]
  praiseWords: string[]
  excludeWords: string[]
}

/**
 * 将底层正则数组拆解还原为干净的纯文字标签数组
 * 自动剥离 (?:...)、.{0,8}、^...$、转义符号，并按 | 拆分为独立纯文字词条，去重去空
 */
// Only literal alternatives can be edited as words. Other expressions remain opaque.
function literalTags(pattern: string): string[] | null {
  const parts: string[] = []
  let word = ''
  for (let i = 0; i < pattern.length; i++) {
    const ch = pattern[i]
    if (ch === '\\') {
      const next = pattern[++i]
      if (!next || !'.*+?^${}()|[]\\'.includes(next)) return null
      word += next
    } else if (ch === '|') {
      if (!word) return null
      parts.push(word); word = ''
    } else if ('.*+?^${}()[]'.includes(ch)) return null
    else word += ch
  }
  return word ? [...parts, word] : null
}

export function patternsToTags(patterns: string[]): string[] {
  return Array.from(new Set(patterns.flatMap((p, i) => literalTags(p) ?? [`已有复杂规则 ${i + 1}`])))
}

export function updatePatterns(previous: string[], tags: string[]): string[] {
  const opaque = new Map<string, string>(previous.flatMap((p, i) => literalTags(p) === null ? [[`已有复杂规则 ${i + 1}`, p] as const] : []))
  const kept = tags.filter(t => opaque.has(t)).map(t => opaque.get(t)!)
  return [...kept, ...tagsToPatterns(tags.filter(t => !opaque.has(t)))]
}

export function mergeSavedEdits<T extends { task?: { human_overrides: string[] } }>(local: T, saved: T, submitted: T): T {
  const result = JSON.parse(JSON.stringify(local)) as T
  if (result.task) {
    const added = (saved.task?.human_overrides || []).filter(p => !submitted.task?.human_overrides.includes(p))
    result.task.human_overrides = Array.from(new Set([...result.task.human_overrides, ...added]))
  }
  return result
}

/**
 * 转义正则特殊字符
 */
function escapeRegex(str: string): string {
  return str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/**
 * 将用户在界面上输入的纯文字标签数组安全转义并封装为底层引擎识别的高效正则列表
 */
export function tagsToPatterns(tags: string[]): string[] {
  if (!Array.isArray(tags)) return []
  const valid = Array.from(new Set(tags.map(t => t.trim()).filter(Boolean)))
  if (valid.length === 0) return []
  // 底层正则引擎使用 re.search(p, text, re.I)，用 | 拼接即可提供最高性能与精确匹配
  return [valid.map(escapeRegex).join('|')]
}

/**
 * 纯文字输入自动双向同步写入底层 Policy 规则、门禁以及 Task 排除项
 */
export function syncSimpleWordsToPolicy(
  policy: any,
  words: {
    topicWords: string[]
    exactSignalWords: string[]
    praiseWords: string[]
    excludeWords: string[]
  },
  task?: any
) {
  if (!policy) return

  // 1. topicWords ➔ 写入 policy.topic_patterns (Schema 要求至少 1 项)
  if (words.topicWords && words.topicWords.length > 0) {
    policy.topic_patterns = tagsToPatterns(words.topicWords)
  } else {
    policy.topic_patterns = ['搭子']
  }

  policy.rules = policy.rules || {}
  policy.decision_gates = policy.decision_gates || {}

  // 2. exactSignalWords ➔ 写入 intent_seeking_company，并彻底清空隐藏的 planning 与 attended
  const exactPatterns = tagsToPatterns(words.exactSignalWords || [])
  policy.rules.intent_seeking_company = exactPatterns
  policy.decision_gates.intent_seeking_company = exactPatterns
  policy.rules.intent_planning = []
  policy.decision_gates.intent_planning = []
  policy.rules.intent_attended = []
  policy.decision_gates.intent_attended = []

  // 3. praiseWords ➔ 写入 policy.rules.stance_positive 和 policy.decision_gates.stance_positive
  const praisePatterns = tagsToPatterns(words.praiseWords || [])
  policy.rules.stance_positive = praisePatterns
  policy.decision_gates.stance_positive = praisePatterns

  // 4. excludeWords ➔ 写入 promotion_yes 和 cancelled_plan，无条件更新
  const excludePatterns = tagsToPatterns(words.excludeWords || [])
  policy.rules.promotion_yes = excludePatterns
  policy.decision_gates.promotion_yes = excludePatterns
  policy.decision_gates.cancelled_plan = excludePatterns

  if (task && task.exclusions) {
    task.exclusions.terms = Array.from(new Set((words.excludeWords || []).map(w => w.trim()).filter(Boolean)))
    task.exclusions.promotions = true
  }
}

export const PRESET_TEMPLATES: PresetTemplate[] = []
