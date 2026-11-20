import { useRef } from 'react'
import { Download, RotateCcw, Upload } from 'lucide-react'
import Card from '../components/Card'
import type { Notices } from '../hooks/useConfigDraft'
import type { Api } from '../lib/apiClient'
import type { Bundle } from '../types'

type Props = { api: Api; config: any; notices: Notices; onRestored: () => void }

export default function HistoryPage({ api, config, notices, onRestored }: Props) {
  const fileRef = useRef<HTMLInputElement>(null)

  const restore = async (revision: string, name: string) => {
    if (!confirm(`确定载入版本「${name}」吗？`)) return
    try {
      config.loadDraft(await api<Bundle>(`/api/versions/${revision}`))
      notices.notify(`已载入版本 ${revision.slice(0, 8)}，保存后生效`)
      onRestored()
    } catch (error) {
      notices.fail((error as Error).message)
    }
  }

  const validateJson = async () => {
    try {
      const parsed = JSON.parse(config.jsonText)
      await api('/api/validate', { bundle: parsed })
      config.loadDraft(parsed)
      notices.notify('JSON 校验通过，已载入草稿')
    } catch (error) {
      notices.fail((error as Error).message)
    }
  }

  return (
    <div className="history">
      <Card title="版本快照" desc="最新的在最上面，恢复后需要再次保存才会生效。" aside={`${config.versions.length} 个`}>
        {config.versions.length === 0 ? (
          <div className="empty">暂无历史版本</div>
        ) : (
          <div className="timeline">
            {config.versions.map((version: any) => (
              <div key={version.revision} className="tl-item">
                <div style={{ minWidth: 0 }}>
                  <b>{version.name}</b>
                  <small>{new Date(version.created_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })} · {version.revision.slice(0, 8)}</small>
                </div>
                <button type="button" className="btn btn-sm" onClick={() => restore(version.revision, version.name)}>
                  <RotateCcw size={12} />恢复
                </button>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card
        title="完整配置 JSON"
        desc="可直接编辑后校验载入，或导入导出文件做备份。"
        aside={
          <div className="top-actions">
            <input
              ref={fileRef}
              type="file"
              accept=".json,application/json"
              hidden
              onChange={e => {
                const file = e.target.files?.[0]
                if (file) void config.importFile(file)
                e.target.value = ''
              }}
            />
            <button type="button" className="btn btn-sm" onClick={() => fileRef.current?.click()}><Upload size={12} />导入</button>
            <button type="button" className="btn btn-sm" onClick={config.exportFile}><Download size={12} />导出</button>
          </div>
        }
      >
        <textarea className="textarea json" value={config.jsonText} onChange={e => config.setJsonText(e.target.value)} rows={20} spellCheck={false} aria-label="配置 JSON" />
        <button type="button" className="btn" style={{ marginTop: 12, width: '100%' }} onClick={validateJson}>
          校验并载入
        </button>
      </Card>
    </div>
  )
}
