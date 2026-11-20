import RunMonitor, { type LiveData } from '../RunMonitor'
import type { RunState } from '../types'

type Props = {
  run: RunState | null
  live: LiveData | null
  error: string
  onSendToTest: (comment: string, context?: string) => void
  onAddBlacklistWord: (input: string) => void
  onRequestOpenLogin: (platformId: string) => void
}

export default function MonitorPage(props: Props) {
  return (
    <RunMonitor
      run={props.run}
      live={props.live}
      error={props.error}
      onSendToTest={props.onSendToTest}
      onAddBlacklistWord={props.onAddBlacklistWord}
      onRequestOpenLogin={props.onRequestOpenLogin}
    />
  )
}
