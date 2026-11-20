import { AlertTriangle, CheckCircle2, X } from 'lucide-react'

type Props = {
  message: string
  error: string
  onCloseMessage: () => void
  onCloseError: () => void
}

export default function Toasts({ message, error, onCloseMessage, onCloseError }: Props) {
  return (
    <div className="toasts" aria-live="polite">
      {message && (
        <div className="toast" role="status">
          <CheckCircle2 size={16} />
          <span>{message}</span>
          <button type="button" onClick={onCloseMessage} aria-label="关闭提示"><X size={14} /></button>
        </div>
      )}
      {error && (
        <div className="toast error" role="alert">
          <AlertTriangle size={16} />
          <span>{error}</span>
          <button type="button" onClick={onCloseError} aria-label="关闭错误"><X size={14} /></button>
        </div>
      )}
    </div>
  )
}
