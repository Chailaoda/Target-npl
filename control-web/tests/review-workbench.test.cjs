const { test, afterEach } = require('node:test')
const assert = require('node:assert/strict')
const path = require('node:path'), Module = require('node:module')
const { JSDOM } = require('jsdom')
const dom = new JSDOM('<html><body></body></html>', { url: 'http://review.invalid' })
Object.assign(global, {
  window: dom.window,
  document: dom.window.document,
  HTMLElement: dom.window.HTMLElement,
  localStorage: dom.window.localStorage,
  IS_REACT_ACT_ENVIRONMENT: true
})
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true })
const React = require('react')
const { render, screen, fireEvent, waitFor, cleanup } = require('@testing-library/react')

function load(entry) {
  const root = path.resolve(__dirname, '..')
  const result = require('esbuild').buildSync({
    entryPoints: [path.join(root, entry)], write: false, bundle: true, format: 'cjs',
    platform: 'node', jsx: 'automatic', alias: { '@': path.join(root, 'src') }, external: ['react', 'react-dom']
  })
  const m = new Module(path.join(root, 'review-test.cjs'), module)
  m.filename = path.join(root, 'review-test.cjs'); m.paths = module.paths
  m._compile(result.outputFiles[0].text, m.filename)
  return m.exports
}

const page = load('src/pages/ReviewWorkbenchPage.tsx')
const ReviewWorkbenchPage = page.default
afterEach(cleanup)

const pending = [
  { analysis_key: 'a'.repeat(64), platform: 'douyin', text: '第一条待复核', stance: 'positive', intent: 'none', score: 60, reason: '模型不确定' },
  { analysis_key: 'b'.repeat(64), platform: 'xhs', text: '第二条待复核', stance: 'mixed', intent: 'seeking', score: 55, reason: '证据不足' }
]
const conflicts = [
  { conflict_id: 1, platform: 'douyin', object_type: 'post', content_id: 'p1', current_key: 'douyin:A', current_authority: 'raw_import', candidate_key: 'douyin:B', candidate_authority: 'platform_recheck', snippet: '第一条冲突原文', status: 'pending' },
  { conflict_id: 2, platform: 'xhs', object_type: 'post', content_id: 'p2', current_key: 'xhs:A', current_authority: 'raw_import', candidate_key: 'xhs:B', candidate_authority: 'platform_recheck', snippet: '第二条冲突原文', status: 'pending' }
]

function setup({ failReview = false } = {}) {
  const calls = []
  global.fetch = async (url, options = {}) => {
    calls.push({ url, body: options.body ? JSON.parse(options.body) : null, method: options.method })
    if (url === '/api/reviews/pending?limit=50') return { ok: true, json: async () => ({ items: pending.map(item => ({ ...item })) }) }
    if (url === '/api/accounts/conflicts?status=pending&limit=50') return { ok: true, json: async () => ({ items: conflicts.map(item => ({ ...item })) }) }
    if (url === '/api/reviews') {
      if (failReview) return { ok: false, json: async () => ({ error: '分析记录不存在' }) }
      return { ok: true, json: async () => ({ updated: 1 }) }
    }
    if (url === '/api/accounts/conflicts/resolve') return { ok: true, json: async () => ({ resolved: true }) }
    throw Error('Unexpected ' + url)
  }
  const failures = []
  const notices = []
  render(React.createElement(ReviewWorkbenchPage, { api: async (p, b) => {
    const response = await fetch(p, b ? { method: 'POST', body: JSON.stringify(b) } : {})
    const data = await response.json()
    if (!response.ok) throw Error(data.error)
    return data
  }, notices: { notify: text => notices.push(text), fail: text => failures.push(text) } }))
  return { calls, failures, notices }
}

const press = key => fireEvent.keyDown(window, { key })
const activeText = selector => document.querySelector(`${selector} li.active`)?.textContent || ''

test('J moves the selection and A accepts the highlighted item', async () => {
  const { calls } = setup()
  await screen.findByText('第一条待复核')
  assert.match(activeText('.review-list'), /第一条待复核/)
  press('j')
  assert.match(activeText('.review-list'), /第二条待复核/)
  press('a')
  await waitFor(() => assert.equal(document.querySelectorAll('.review-list li').length, 1))
  const review = calls.filter(call => call.url === '/api/reviews')
  assert.equal(review.length, 1)
  assert.deepEqual(review[0].body.items, [{ analysis_key: 'b'.repeat(64), decision: 'accept' }])
})

test('a failed submission rolls the item back and asks the operator to retry', async () => {
  const { failures } = setup({ failReview: true })
  await screen.findByText('第一条待复核')
  press('r')
  await waitFor(() => assert.equal(document.querySelectorAll('.review-list li').length, 2))
  assert.ok(failures.some(text => text.includes('分析记录不存在')))
})

test('conflict pane resolves with the candidate key and removes the row', async () => {
  const { calls } = setup()
  await screen.findByText('第一条冲突原文')
  press('f')
  assert.match(activeText('.conflict-list'), /第一条冲突原文/)
  press('2')
  await waitFor(() => assert.equal(document.querySelectorAll('.conflict-list li').length, 1))
  const resolve = calls.filter(call => call.url === '/api/accounts/conflicts/resolve')
  assert.equal(resolve.length, 1)
  assert.equal(resolve[0].body.chosen_key, 'douyin:B')
  assert.equal(resolve[0].body.platform, 'douyin')
})

test('arrow keys and typing in inputs never trigger review shortcuts', async () => {
  const { calls } = setup()
  await screen.findByText('第一条待复核')
  const input = document.createElement('input')
  document.body.appendChild(input)
  fireEvent.keyDown(input, { key: 'a' })
  await new Promise(resolve => setTimeout(resolve, 30))
  assert.equal(calls.filter(call => call.url === '/api/reviews').length, 0)
  input.remove()
})

test('keyboard helpers are bounded and ignore editable targets', () => {
  assert.equal(page.nextIndex(0, -1, 3), 0)
  assert.equal(page.nextIndex(2, 1, 3), 2)
  assert.equal(page.nextIndex(1, 1, 3), 2)
  assert.equal(page.nextIndex(0, 1, 0), 0)
  assert.equal(page.isEditableTarget({ tagName: 'INPUT' }), true)
  assert.equal(page.isEditableTarget({ tagName: 'TEXTAREA' }), true)
  assert.equal(page.isEditableTarget({ tagName: 'DIV' }), false)
  assert.equal(page.isEditableTarget(null), false)
})
