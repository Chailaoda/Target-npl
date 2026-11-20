// WCAG AA and theme-contract checks for the design tokens (WP8-T3).
const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs'), path = require('node:path')

const css = fs.readFileSync(path.resolve(__dirname, '../src/index.css'), 'utf8')

function block(marker) {
  const start = css.indexOf(marker)
  assert.ok(start >= 0, `missing selector ${marker}`)
  const open = css.indexOf('{', start)
  const close = css.indexOf('}', open)
  return css.slice(open + 1, close)
}

function tokens(marker) {
  const out = {}
  for (const line of block(marker).split(/\r?\n/)) {
    const match = line.match(/--([\w-]+)\s*:\s*([^;]+);/)
    if (match) out[match[1]] = match[2].trim()
  }
  return out
}

function rgb(value) {
  const hex = value.replace('#', '').trim()
  assert.equal(hex.length, 6, `expected 6-digit hex, got ${value}`)
  return [0, 2, 4].map(index => parseInt(hex.slice(index, index + 2), 16))
}

function luminance(value) {
  const [r, g, b] = rgb(value).map(channel => {
    const c = channel / 255
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

function contrast(a, b) {
  const [x, y] = [luminance(a), luminance(b)].sort((m, n) => n - m)
  return (x + 0.05) / (y + 0.05)
}

const light = tokens("data-theme='light'")
const dark = tokens("data-theme='dark'")

test('light theme is the documented default', () => {
  assert.match(css, /:root,\s*\r?\n:root\[data-theme='light'\]/)
  assert.equal(light['bg'], '#f8fafc')
  assert.equal(light['surface'], '#ffffff')
  assert.equal(light['accent'], '#1d4ed8')
})

test('dark theme is soft charcoal, not high-glare neon', () => {
  assert.equal(dark['bg'], '#111827')
  assert.equal(dark['surface'], '#18202f')
  assert.ok(!css.includes('#2ee6a6'), 'saturated mint accent must be gone')
  assert.ok(!css.includes('#090a0c'), 'near-black background must be gone')
})

for (const [name, theme] of [['light', light], ['dark', dark]]) {
  test(`${name} theme meets WCAG AA for body and secondary text`, () => {
    for (const surface of ['bg', 'panel', 'surface']) {
      for (const ink of ['text', 'text-2', 'text-3']) {
        const ratio = contrast(theme[ink], theme[surface])
        assert.ok(ratio >= 4.5, `${ink} on ${surface} = ${ratio.toFixed(2)}:1`)
      }
    }
  })

  test(`${name} theme keeps accent and on-accent legible`, () => {
    assert.ok(contrast(theme.accent, theme.bg) >= 3, 'accent must stay distinguishable from the page')
    assert.ok(contrast(theme['on-accent'], theme.accent) >= 4.5, 'text on an accent button must be AA')
  })
}

test('numbers use tabular figures', () => {
  assert.match(css, /font-variant-numeric:\s*tabular-nums/)
})
