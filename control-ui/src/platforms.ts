export type PlatformMeta = { name: string; glyph: string; color: string }

export const PLATFORM_META: Record<string, PlatformMeta> = {
  douyin: { name: '抖音', glyph: '抖', color: '#ff4d6d' },
  xhs: { name: '小红书', glyph: '红', color: '#ff8f3c' },
  kuaishou: { name: '快手', glyph: '快', color: '#f5c542' },
  bilibili: { name: 'B站', glyph: 'B', color: '#4fc3f7' },
  weibo: { name: '微博', glyph: '微', color: '#c084fc' },
  tieba: { name: '百度贴吧', glyph: '贴', color: '#2dd4bf' },
  zhihu: { name: '知乎', glyph: '知', color: '#818cf8' }
}

export const platformNames: Record<string, string> = Object.fromEntries(
  Object.entries(PLATFORM_META).map(([id, meta]) => [id, meta.name])
)

export const platformMeta = (id: string): PlatformMeta =>
  PLATFORM_META[id] || { name: id, glyph: id.slice(0, 1).toUpperCase(), color: '#94a3b8' }
