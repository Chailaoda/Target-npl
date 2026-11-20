// P1-1 regression: the Web workbench top pill (RunMonitor.run-head) must
// never show an incomplete collection as "已完成". taskStatusLabel is the
// single source of that label, so assert the display contract directly.
const { test } = require('node:test')
const assert = require('node:assert/strict')
const path = require('node:path'), Module = require('node:module')

function load(entry) {
  const root = path.resolve(__dirname, '..')
  const result = require('esbuild').buildSync({
    entryPoints: [path.join(root, entry)], write: false, bundle: true, format: 'cjs',
    platform: 'node', jsx: 'automatic', alias: { '@': path.join(root, 'src') },
    external: ['react', 'react-dom'],
  })
  const m = new Module(path.join(root, 'partial-status-test.cjs'), module)
  m.filename = path.join(root, 'partial-status-test.cjs'); m.paths = module.paths
  m._compile(result.outputFiles[0].text, m.filename)
  return m.exports
}

const { taskStatusLabel, feedNeedsReset } = load('src/RunMonitor.tsx')

test('partial overall status is displayed as 部分完成', () => {
  assert.equal(taskStatusLabel('partial'), '部分完成')
  assert.notEqual(taskStatusLabel('partial'), '已完成')
})

test('a completed status is downgraded when collection_incomplete is set', () => {
  assert.equal(taskStatusLabel('completed'), '已完成')
  assert.equal(taskStatusLabel('completed', true), '部分完成（采集不完整）')
  assert.equal(taskStatusLabel('completed_empty', true), '部分完成（采集不完整）')
  assert.equal(taskStatusLabel('completed_with_review', true), '部分完成（采集不完整）')
})

test('a failed analysis keeps its failure status even when collection is incomplete', () => {
  assert.equal(taskStatusLabel('failed'), '失败')
  assert.equal(taskStatusLabel('failed', true), '失败')
  assert.equal(taskStatusLabel('needs_user_login', true), '等待本人登录')
})

test('unknown status falls back to the raw value instead of claiming success', () => {
  assert.equal(taskStatusLabel('some_new_status'), 'some_new_status')
  assert.equal(taskStatusLabel('some_new_status', true), 'some_new_status')
})

test('a shrinking authoritative total forces the temporary feed to reset', () => {
  assert.equal(feedNeedsReset(null, 0), false)
  assert.equal(feedNeedsReset(271, 271), false)
  assert.equal(feedNeedsReset(271, 357), false)
  assert.equal(feedNeedsReset(271, 0), true)
  assert.equal(feedNeedsReset(357, 271), true)
})
