import { useState, type CSSProperties } from 'react'
import { ChevronDown, FlaskConical, Loader2, Lock, Sparkles } from 'lucide-react'
import Card from '../components/Card'
import TagInput from '../components/TagInput'
import PlatformPicker from '../PlatformPicker'
import { PRESET_TEMPLATES } from '../presets'
import { makeTask } from '../types'
import type { Notices } from '../hooks/useConfigDraft'
import type { Sandbox } from '../hooks/useSandbox'
import type { Api } from '../lib/apiClient'

type Props = {
  api: Api
  config: any
  sandbox: Sandbox
  busy: boolean
  notices: Notices
  loginNeeded: string[]
}

export default function WorkbenchPage({ api, config, sandbox, busy, notices, loginNeeded }: Props) {
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const draft = config.draft
  const task = draft.task!
  const verdict = sandbox.result
    ? sandbox.result.accepted
      ? sandbox.result.evidence_level === 'explicit' || sandbox.result.quality_flags?.includes('tier_exact')
        ? { label: '精准目标', tier: 'var(--exact)' }
        : { label: '潜在沾边', tier: 'var(--potential)' }
      : { label: '已剔除', tier: 'var(--danger)' }
    : null

  return (
    <div className="workbench">
      <div className="stack">
        <Card step="01" title="筛选目标" desc="用一句话描述要找的人，AI 会提炼出搜索词和判定词库；关键词与规则由使用者在本机配置。">
          <div className="presets">
            {PRESET_TEMPLATES.map(preset => (
              <button key={preset.id} type="button" className="preset" onClick={() => config.applyPreset(preset)}>
                <b>{preset.name}</b>
                <span>{preset.description}</span>
              </button>
            ))}
          </div>
          <div className="goal">
            <input
              className="input input-lg"
              value={config.goalText}
              onChange={e => {
                config.setGoalText(e.target.value)
                config.updateDraft((b: any) => { if (b.task) b.task.description = e.target.value })
              }}
              placeholder="输入要分析的公开内容主题与证据条件"
              aria-label="筛选目标"
            />
            <button type="button" className="btn" style={{ height: 44 }} onClick={config.handleAIGenerate} disabled={busy || !config.goalText.trim()}>
              <Sparkles size={14} style={{ color: 'var(--accent)' }} />AI 提炼词库
            </button>
          </div>
          {task.human_overrides.length > 0 && (
            <div className="lock-note">
              <span><Lock size={13} />{task.human_overrides.length} 项人工修改已锁定，AI 生成时不会覆盖</span>
              <button type="button" onClick={() => { config.updateDraft((b: any) => { if (b.task) b.task.human_overrides = [] }); notices.notify('已解除全部人工锁定') }}>
                全部解锁
              </button>
            </div>
          )}
        </Card>

        <Card step="02" title="词库" desc="回车添加，支持一次粘贴多个；会自动写入底层规则，不需要写正则。">
          <TagInput label="平台搜索关键词" hint="送到各平台搜索框召回内容" tags={draft.collection.keywords || []} onChange={(tags: string[]) => config.updateDraft((b: any) => { b.collection.keywords = tags })} placeholder="如：示例活动搭子、找同好" tone="accent" />
          <TagInput label="主题核心词" hint="内容必须围绕的话题，评论可继承原帖主题" tags={config.topicWords} onChange={(tags: string[]) => config.handleUpdateWords('topic', tags)} placeholder="如：示例活动、打球、球馆" tone="violet" />
          <TagInput label="精准信号词" hint="找搭子、求带等明确表达，命中归入精准目标" tags={config.exactSignalWords} onChange={(tags: string[]) => config.handleUpdateWords('exact', tags)} placeholder="如：找搭子、求带、缺人、求拉群" tone="exact" />
          <TagInput label="潜在信号词" hint="正向赞美或感兴趣，命中归入潜在沾边" tags={config.praiseWords} onChange={(tags: string[]) => config.handleUpdateWords('praise', tags)} placeholder="如：太帅了、好喜欢、种草、想去" tone="potential" />
          <TagInput label="黑名单排除词" hint="出现即一票否决，同时拦截商业推广" tags={config.excludeWords} onChange={(tags: string[]) => config.handleUpdateWords('exclude', tags)} placeholder="如：私信领福利、代练、卖课、加微" tone="danger" />
        </Card>

        <Card step="03" title="平台与范围" desc="采集全程在后台进行；只有需要登录或验证时才会弹出对应平台的浏览器。">
          <PlatformPicker
            selected={draft.collection.platforms || [draft.collection.platform]}
            request={api}
            loginNeeded={loginNeeded}
            onNotice={(text: string, isError?: boolean) => (isError ? notices.fail(text) : notices.notify(text))}
            onChange={(ids: string[]) => config.updateDraft((b: any) => {
              b.collection.platforms = ids
              if (!ids.includes(b.collection.platform)) b.collection.platform = ids[0]
            })}
          />
          <div className="divider" />
          <div className="grid-2">
            <TagInput label="活动地点" hint="正文或原帖提到才保留，留空不限" tags={task.scope.locations} onChange={(tags: string[]) => config.updateDraft((b: any) => { (b.task ??= makeTask(b)).scope.locations = tags })} placeholder="如：示例城市甲、示例城市乙" tone="neutral" />
            <label className="field">
              <span className="field-label">最近天数<small>留空不限时间</small></span>
              <input
                className="input"
                type="number"
                min={1}
                max={3650}
                value={task.scope.recent_days ?? ''}
                onChange={e => config.updateDraft((b: any) => { (b.task ??= makeTask(b)).scope.recent_days = e.target.value ? Number(e.target.value) : null })}
                placeholder="不限"
              />
            </label>
          </div>
          <div className="divider" />
          <button type="button" className="disclosure" aria-expanded={advancedOpen} onClick={() => setAdvancedOpen(!advancedOpen)}>
            高级额度与分析方式
            <small>作品 {draft.collection.max_posts} · 单帖评论 {draft.collection.comments_per_post} · 请求 {draft.collection.max_requests} · 模型 {task.execution.max_api_calls} 次</small>
            <ChevronDown size={15} />
          </button>
          {advancedOpen && (
            <div className="adv-grid">
              {([
                ['作品总上限', 'max_posts', 1, 500],
                ['单帖评论上限', 'comments_per_post', 0, 100],
                ['每词页数', 'pages_per_keyword', 1, 20],
                ['请求上限', 'max_requests', 1, 500]
              ] as const).map(([label, key, min, max]) => (
                <label key={key} className="field">
                  <span className="field-label">{label}</span>
                  <input className="input mono" type="number" min={min} max={max} value={draft.collection[key]} onChange={e => config.updateDraft((b: any) => { b.collection[key] = Number(e.target.value) })} />
                </label>
              ))}
              <label className="field span-2">
                <span className="field-label">数据来源</span>
                <select className="select" value={task.execution.source} onChange={e => config.updateDraft((b: any) => { (b.task ??= makeTask(b)).execution.source = e.target.value as 'collect' | 'existing' })}>
                  <option value="collect">新增多平台采集</option>
                  <option value="existing">复用已有采集数据（免费重筛）</option>
                </select>
              </label>
              <label className="field span-2">
                <span className="field-label">分析方式</span>
                <select className="select" value={task.execution.mode} onChange={e => config.updateDraft((b: any) => { (b.task ??= makeTask(b)).execution.mode = e.target.value as 'ark' | 'rules' })}>
                  <option value="ark">方舟大模型 + 本地规则</option>
                  <option value="rules">仅本地规则（不消耗模型额度）</option>
                </select>
              </label>
              {task.execution.source === 'existing' && (
                <label className="field span-2">
                  <span className="field-label">复用批次</span>
                  <input className="input mono" value={task.execution.dataset_id} onChange={e => config.updateDraft((b: any) => { (b.task ??= makeTask(b)).execution.dataset_id = e.target.value })} placeholder="latest 或批次时间戳" />
                </label>
              )}
              <label className="field span-2">
                <span className="field-label">模型调用预算</span>
                <input className="input mono" type="number" min={1} max={500} value={task.execution.max_api_calls} onChange={e => config.updateDraft((b: any) => { (b.task ??= makeTask(b)).execution.max_api_calls = Number(e.target.value) })} />
              </label>
            </div>
          )}
        </Card>
      </div>

      <div className="sandbox">
        <Card title="试金石" desc="用当前词库即时判定一句话会落到哪一栏，不会保存或采集。" aside={<FlaskConical size={15} />}>
          <div className="stack" style={{ gap: 14 }}>
            <label className="field">
              <span className="field-label">原帖标题 / 上下文<small>可选</small></span>
              <textarea className="textarea" rows={2} value={sandbox.contextText} onChange={e => sandbox.setContextText(e.target.value)} placeholder="如：五一示例城市甲示例活动馆约球" />
            </label>
            <label className="field">
              <span className="field-label">待测发言</span>
              <textarea className="textarea" rows={3} value={sandbox.commentText} onChange={e => sandbox.setCommentText(e.target.value)} placeholder="如：有人一起吗？求搭子" />
            </label>
            <button type="button" className="btn" onClick={() => sandbox.run(draft)} disabled={sandbox.busy || !sandbox.commentText.trim()}>
              {sandbox.busy ? <Loader2 size={14} className="spin" /> : <FlaskConical size={14} />}
              {sandbox.busy ? '判定中…' : '运行判定'}
            </button>
          </div>
          {sandbox.result && verdict && (
            <div className="verdict" style={{ '--tier': verdict.tier } as CSSProperties}>
              <span className="verdict-tag">{verdict.label}</span>
              <dl>
                <dt>主题</dt><dd>{sandbox.result.topic}</dd>
                <dt>意向</dt><dd>{sandbox.result.intent}</dd>
                <dt>态度</dt><dd>{sandbox.result.stance}</dd>
                <dt>推荐分</dt><dd style={{ color: verdict.tier }}>{sandbox.result.score ?? '—'}</dd>
              </dl>
              <p>{sandbox.result.reason}</p>
              {sandbox.result.scope_check && sandbox.result.scope_check.status !== 'matched' && (
                <div className="scope">范围：{sandbox.result.scope_check.reasons.join('；')}</div>
              )}
            </div>
          )}
        </Card>
      </div>
    </div>
  )
}
