// Incremental merge must accept the opaque cursor, dedupe ids and stay bounded.
const { test } = require('node:test')
const assert = require('node:assert/strict')
const path = require('node:path'), Module = require('node:module')

function load(entry) {
  const root = path.resolve(__dirname, '..')
  const result = require('esbuild').buildSync({
    entryPoints: [path.join(root, entry)], write: false, bundle: true, format: 'cjs',
    platform: 'node', jsx: 'automatic', alias: { '@': path.join(root, 'src') },
    external: ['react', 'react-dom']
  })
  const m = new Module(path.join(root, 'live-merge-test.cjs'), module)
  m.filename = path.join(root, 'live-merge-test.cjs'); m.paths = module.paths
  m._compile(result.outputFiles[0].text, m.filename)
  return m.exports
}

const { mergeLive } = load('src/hooks/useRunStream.ts')
const row = id => ({ id, platform: 'douyin', kind: 'post', text: id })
const previous = {
  run_id: 'run', status: 'running', captured: Array.from({ length: 400 }, (_, i) => row(`old-${i}`)),
  captured_total: 400, candidates: [], candidate_total: 0, processed: 0, needs_review: 0,
  collection: {}, report_url: null
}

test('string cursor merges deltas, dedupes and keeps the feed bounded', () => {
  const merged = mergeLive(previous, {
    run_id: 'run', incremental: true, next_seq: 'opaque-token',
    captured_delta: [row('old-399'), row('new-1'), row('new-2')],
    candidates_delta: []
  })
  assert.equal(merged.captured.length, 400)
  assert.equal(merged.captured.at(-1).id, 'new-2')
  assert.equal(merged.captured.filter(item => item.id === 'new-2').length, 1)
})

test('a full snapshot replaces previous rows instead of appending', () => {
  const merged = mergeLive(previous, {
    run_id: 'run', incremental: false, captured: [row('fresh')], captured_total: 1,
    candidates: [], candidate_total: 0, processed: 0, needs_review: 0,
    collection: {}, report_url: null
  })
  assert.deepEqual(merged.captured.map(item => item.id), ['fresh'])
})
