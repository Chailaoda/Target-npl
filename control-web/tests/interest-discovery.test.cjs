// Complete WP6 path: topic -> analyze -> results -> account -> evidence -> feedback.
const { test, beforeEach, afterEach } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs'), path = require('node:path'), Module = require('node:module')
const { JSDOM } = require('jsdom')
const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://component-test.invalid' })
Object.assign(global, { window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement, localStorage: dom.window.localStorage, IS_REACT_ACT_ENVIRONMENT: true })
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true })
const React = require('react')
const { render, screen, waitFor, cleanup, fireEvent } = require('@testing-library/react')
function load(entry) {
  const root = path.resolve(__dirname, '..')
  const result = require('esbuild').buildSync({ entryPoints: [path.join(root, entry)], write: false, bundle: true, format: 'cjs', platform: 'node', jsx: 'automatic', alias: { '@': path.join(root, 'src') }, external: ['react', 'react-dom'] })
  const m = new Module(path.join(root, 'interest-test.cjs'), module); m.filename = path.join(root, 'interest-test.cjs'); m.paths = module.paths
  m._compile(result.outputFiles[0].text, m.filename); return m.exports
}
const { default: App, makeTask } = load('src/App.tsx')
const read = f => JSON.parse(fs.readFileSync(path.resolve(__dirname, '../..', f), 'utf8').replace(/^\uFEFF/, ''))
const baseline = { policy: read('profiles/default.json'), collection: read('config/collection.json') }
baseline.task = makeTask(baseline)
const candidate = {
  account_key: 'bilibili:A', account_id: 'A', platform: 'bilibili', nickname: '公开账号甲', profile_url: 'https://space.bilibili.com/A',
  topic: '胶片摄影', interest_score: 42, stance: 'positive', evidence_count: 2, independent_content_count: 2,
  active_days: 2, coverage: 'medium', trend: 'stable', last_activity: '2026-11-09T00:00:00+00:00',
  updated_at: '2026-11-10T00:00:00+00:00', synthetic: true,
  representative_evidence: { quote: '我喜欢胶片摄影，这是我的拍摄经验。', source_url: 'https://www.bilibili.com/video/a1', published_display: '2026-11-09T00:00:00+00:00' }
}
const result = { topic: '胶片摄影', status: 'completed', items: [candidate], total: 1, limit: 12, offset: 0, analysis: { stats: { accounts: 1, candidate_contents: 2, empty_public_account_data: false } } }
const account = {
  account: { account_key: 'bilibili:A', platform: 'bilibili', account_id: 'A', nickname: '公开账号甲', profile_url: 'https://space.bilibili.com/A' },
  content_count: 2, active_days: 2, attribution_conflicts: [], scope_note: '仅基于已采集且有可靠公开账号 ID 的内容；未观测不等于没有兴趣。',
  interests: [{ topic: '胶片摄影', interest_score: 42, stance: 'positive', evidence_count: 2, independent_content_count: 2, active_days: 2, coverage: 'medium', trend: 'stable', last_activity: '2026-11-09T00:00:00+00:00', profile_version: 'interest-v1:test', updated_at: '2026-11-10T00:00:00+00:00', synthetic: true, windows: {} }]
}
const evidenceItem = {
  id: 7, object_type: 'post', platform: 'bilibili', quote: '我喜欢胶片摄影，这是我的拍摄经验。', full_text: '我喜欢胶片摄影，这是我的拍摄经验。',
  published_display: '2026-11-09T00:00:00+00:00', relevance: 1, depth: .8, quality: .95, stance: 'positive', contribution: 3.04,
  raw_score: 3.04, source_url: 'https://www.bilibili.com/video/a1', quality_flags: [], review_state: 'accepted', review_note: ''
}
const evidence = { account_key: 'bilibili:A', topic: '胶片摄影', profile_version: 'interest-v2:test', interest_score: 42, stance: 'positive', evidence_count: 1, independent_content_count: 1, active_days: 1, coverage: 'low', trend: 'insufficient_data', last_activity: '2026-11-09T00:00:00+00:00', score_note: '兴趣参与指数不是概率。', items: [evidenceItem] }
let calls, feedbacks
beforeEach(() => {
  calls = []; feedbacks = []; localStorage.clear()
  global.fetch = async (url, options = {}) => {
    calls.push(url)
    const body = options.body ? JSON.parse(options.body) : null
    const respond = data => ({ ok: true, json: async () => JSON.parse(JSON.stringify(data)) })
    if (url === '/api/state') return respond({ bundle: baseline, revision: 'baseline', etag: 'e0', token: 't' })
    if (url === '/api/versions') return respond([])
    if (url === '/api/tasks/latest') return respond(null)
    if (url.startsWith('/api/interests/search?')) return respond(result)
    if (url === '/api/interests/analyze') { assert.equal(body.topic, '胶片摄影'); return respond(result) }
    if (url === '/api/accounts/bilibili:A' || url === '/api/accounts/bilibili%3AA') return respond(account)
    if (url.startsWith('/api/accounts/bilibili:A/interests/') || url.startsWith('/api/accounts/bilibili%3AA/interests/')) return respond(evidence)
    if (url === '/api/interests/evidence-feedback') {
      feedbacks.push(body)
      return respond({ ...account, interests: [{ ...account.interests[0], interest_score: 0, evidence_count: 0 }] })
    }
    throw Error('Unexpected request: ' + url)
  }
})
afterEach(() => cleanup())

test('discovery shows existing data status and avoids automatic analysis', async () => {
  render(React.createElement(App))
  fireEvent.click(await screen.findByRole('button', { name: '人物发现' }))
  assert.ok(await screen.findByText('先输入兴趣主题'))
  assert.equal(calls.some(u => u.startsWith('/api/interests/search?') || u === '/api/interests/analyze'), false)
})

test('analysis re-queries with the currently selected filters', async () => {
  const view = render(React.createElement(App))
  fireEvent.click(await screen.findByRole('button', { name: '人物发现' }))
  fireEvent.change(await screen.findByPlaceholderText('如：胶片摄影、徒步露营、古典音乐'), { target: { value: '胶片摄影' } })
  const scoreInput = view.container.querySelector('input[type="number"]')
  assert.ok(scoreInput)
  fireEvent.change(scoreInput, { target: { value: '40' } })
  fireEvent.click(screen.getByRole('button', { name: '分析已有数据' }))
  await waitFor(() => assert.ok(calls.some(u => u.startsWith('/api/interests/search?') && u.includes('min_score=40'))))
  assert.ok(await screen.findByText('公开账号甲'))
})

test('topic input flows through results, profile, evidence and feedback', async () => {
  render(React.createElement(App))
  fireEvent.click(await screen.findByRole('button', { name: '人物发现' }))
  const input = await screen.findByPlaceholderText('如：胶片摄影、徒步露营、古典音乐')
  fireEvent.change(input, { target: { value: '胶片摄影' } })
  fireEvent.click(screen.getByRole('button', { name: '分析已有数据' }))
  assert.ok(await screen.findByText('公开账号甲'))
  assert.ok(screen.getByText('兴趣参与指数'))
  fireEvent.click(screen.getByLabelText('查看 bilibili:A 的 胶片摄影 评分证据'))
  assert.ok(await screen.findByText('胶片摄影 · 评分依据'))
  assert.ok(await screen.findByText('贡献 3.04'))
  await waitFor(() => assert.ok(screen.getByRole('button', { name: '不相关' })))
  fireEvent.click(screen.getByRole('button', { name: '不相关' }))
  await waitFor(() => assert.equal(feedbacks.length, 1))
  assert.equal(feedbacks[0].evidence_id, 7)
  assert.equal(feedbacks[0].action, 'irrelevant')
  assert.equal(feedbacks[0].profile_version, 'interest-v2:test')
  assert.ok(calls.some(u => u.startsWith('/api/interests/search?')))
})
