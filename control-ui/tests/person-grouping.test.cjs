// P0 regression: person grouping must never merge author-less records.
// Covers author_key="" / undefined / null (falsy trio) plus cross-platform
// anonymous-hash separation. Pure-function tests, no DOM needed.
const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs'), path = require('node:path'), Module = require('node:module')

function load(entry) {
  const root = path.resolve(__dirname, '..')
  const result = require('esbuild').buildSync({
    entryPoints: [path.join(root, entry)], write: false, bundle: true, format: 'cjs',
    platform: 'node', jsx: 'automatic', alias: { '@': path.join(root, 'src') },
    external: ['react', 'react-dom'],
  })
  const m = new Module(path.join(root, 'person-key-test.cjs'), module)
  m.filename = path.join(root, 'person-key-test.cjs'); m.paths = module.paths
  m._compile(result.outputFiles[0].text, m.filename)
  return m.exports
}

const { personGroupKey } = load('src/RunMonitor.tsx')

test('empty-string author_key falls back to per-content key', () => {
  const a = personGroupKey({ platform: 'bilibili', author_key: '', id: 'post-1' })
  const b = personGroupKey({ platform: 'bilibili', author_key: '', id: 'post-2' })
  assert.equal(a, 'bilibili:content:post-1')
  assert.equal(b, 'bilibili:content:post-2')
  assert.notEqual(a, b, 'author-less records must NEVER merge into one person')
})

test('undefined author_key falls back to per-content key', () => {
  const a = personGroupKey({ platform: 'xhs', id: 'note-1' })
  const b = personGroupKey({ platform: 'xhs', id: 'note-2' })
  assert.equal(a, 'xhs:content:note-1')
  assert.notEqual(a, b)
})

test('null author_key falls back to per-content key', () => {
  const a = personGroupKey({ platform: 'douyin', author_key: null, id: 'v1' })
  assert.equal(a, 'douyin:content:v1')
})

test('same anonymous hash on different platforms stays separate', () => {
  const a = personGroupKey({ platform: 'douyin', author_key: 'anonymous:abc', id: '1' })
  const b = personGroupKey({ platform: 'xhs', author_key: 'anonymous:abc', id: '2' })
  assert.equal(a, 'douyin:anonymous:abc')
  assert.equal(b, 'xhs:anonymous:abc')
  assert.notEqual(a, b)
})

test('public account key keeps platform namespace', () => {
  assert.equal(personGroupKey({ platform: 'bilibili', author_key: 'bilibili:42', id: '9' }), 'bilibili:bilibili:42')
})

test('grouping many author-less records produces distinct keys', () => {
  const items = Array.from({ length: 5 }, (_, i) => ({ platform: 'weibo', author_key: '', id: `w-${i}` }))
  const keys = new Set(items.map(personGroupKey))
  assert.equal(keys.size, 5, 'five author-less records must stay five distinct people')
})
