import { useState, type CSSProperties, type KeyboardEvent, type ClipboardEvent } from 'react'
import { X, Plus } from 'lucide-react'

export type Tone = 'accent' | 'violet' | 'exact' | 'potential' | 'danger' | 'neutral'

const toneVar: Record<Tone, string> = {
  accent: 'var(--accent)',
  violet: 'var(--violet)',
  exact: 'var(--exact)',
  potential: 'var(--potential)',
  danger: 'var(--danger)',
  neutral: 'var(--text-2)'
}

export interface TagInputProps {
  label: string
  hint?: string
  tags: string[]
  onChange: (tags: string[]) => void
  placeholder?: string
  tone?: Tone
}

export default function TagInput({ label, hint, tags = [], onChange, placeholder = '输入后回车添加，支持粘贴多个', tone = 'accent' }: TagInputProps) {
  const [value, setValue] = useState('')

  // 按换行、逗号、空格批量拆分
  const addTags = (raw: string) => {
    const items = raw.split(/[,\n，\s]+/).map(item => item.trim()).filter(item => item && !tags.includes(item))
    if (items.length > 0) onChange([...tags, ...Array.from(new Set(items))])
    setValue('')
  }

  const handleKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      e.preventDefault()
      addTags(value)
    } else if (e.key === 'Backspace' && !value && tags.length > 0) {
      e.preventDefault()
      onChange(tags.slice(0, -1))
    }
  }

  const handlePaste = (e: ClipboardEvent<HTMLInputElement>) => {
    e.preventDefault()
    addTags(e.clipboardData.getData('text'))
  }

  return (
    <div className="tagfield" style={{ '--tone': toneVar[tone] } as CSSProperties}>
      <div className="tagfield-head">
        <div className="tagfield-title">
          <span className="tagfield-label"><i />{label}</span>
          {hint && <span className="tagfield-hint">{hint}</span>}
        </div>
        <div className="tagfield-meta">
          <span>{tags.length} 词</span>
          {tags.length > 0 && <button type="button" onClick={() => onChange([])}>清空</button>}
        </div>
      </div>
      <div className="tagbox">
        {tags.map((tag, idx) => (
          <span key={`${tag}-${idx}`} className="chip">
            {tag}
            <button type="button" onClick={() => onChange(tags.filter((_, i) => i !== idx))} aria-label={`移除 ${tag}`}>
              <X size={12} />
            </button>
          </span>
        ))}
        <input
          type="text"
          value={value}
          onChange={e => setValue(e.target.value)}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          onBlur={() => value && addTags(value)}
          placeholder={tags.length === 0 ? placeholder : '继续添加…'}
          aria-label={label}
        />
        {value.trim() && (
          <button type="button" className="chip-add" onClick={() => addTags(value)}>
            <Plus size={12} style={{ display: 'inline', marginRight: 2 }} />添加
          </button>
        )}
      </div>
    </div>
  )
}
