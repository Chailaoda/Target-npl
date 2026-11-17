// Direct RunMonitor regression: a preview from a child collection must not
// remain in the feed after the authoritative parent batch resets to zero.
const { test, afterEach } = require('node:test')
const assert = require('node:assert/strict')
const path = require('node:path'), Module = require('node:module')
const { JSDOM } = require('jsdom')

const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://feed-reset.invalid' })
Object.assign(global, {
  window: dom.window, document: dom.window.document,
  HTMLElement: dom.window.HTMLElement, localStorage: dom.window.localStorage,
  IS_REACT_ACT_ENVIRONMENT: true,
})
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true })
const React = require('react')
const { render, screen, waitFor, cleanup } = require('@testing-library/react')

function load(entry) {
  const root = path.resolve(__dirname, '..')
  const result = require('esbuild').buildSync({
    entryPoints: [path.join(root, entry)], write: false, bundle: true, format: 'cjs',
    platform: 'node', jsx: 'automatic', alias: { '@': path.join(root, 'src') },
    external: ['react', 'react-dom'],
  })
  const m = new Module(path.join(root, 'feed-reset-test.cjs'), module)
  m.filename = path.join(root, 'feed-reset-test.cjs'); m.paths = module.paths
  m._compile(result.outputFiles[0].text, m.filename)
  return m.exports
}

const { default: RunMonitor } = load('src/RunMonitor.tsx')
afterEach(() => cleanup())

test('a shrinking authoritative total clears temporary child rows', async () => {
  const run = { run_id: 'cd'.repeat(16), status: 'running', phase: 'collecting' }
  const preview = {
    run_id: run.run_id, status: 'running', captured: [
      { id: 'child-1', platform: 'douyin', kind: 'comment', text: '临时子批次记录', keyword: '搭子' },
    ],
    captured_total: 271, candidates: [], candidate_total: 0, processed: 0, needs_review: 0,
    collection: { status: 'running', requests: 30, platforms: ['douyin'] }, report_url: null,
  }
  const authoritative = {
    run_id: run.run_id, status: 'failed', captured: [], captured_total: 0,
    candidates: [], candidate_total: 0, processed: 0, needs_review: 0,
    collection: { status: 'failed', requests: 30, platforms: ['douyin'] }, report_url: null,
  }
  const view = render(React.createElement(RunMonitor, { run, live: preview }))
  assert.ok(screen.getByText('临时子批次记录'))

  view.rerender(React.createElement(RunMonitor, {
    run: { ...run, status: 'failed' }, live: authoritative,
  }))
  await waitFor(() => assert.equal(screen.queryByText('临时子批次记录'), null))
  assert.ok(screen.getByText(/未合并的临时记录已从信息流清除/))
})

test('non-fatal collection warnings remain visible on a completed batch', () => {
  const run = { run_id: 'ef'.repeat(16), status: 'completed', phase: 'finished' }
  const live = {
    run_id: run.run_id, status: 'completed', captured: [], captured_total: 0,
    candidates: [], candidate_total: 0, processed: 0, needs_review: 0,
    collection: {
      status: 'completed', requests: 30, warning_count: 1,
      warnings: [{ platform: 'douyin', code: 'COMMENT_DATA_MISSING', post_id: 'p1' }],
    },
    report_url: null,
  }
  render(React.createElement(RunMonitor, { run, live }))
  assert.ok(screen.getByText(/本批次有 1 条作品的评论数据异常/))
})
