import { Component } from 'react'
import type { ReactNode, ErrorInfo } from 'react'

interface Props {
  messageId: string
  children: ReactNode
}

interface State {
  hasError: boolean
  error: Error | null
}

export class MessageErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props)
    this.state = { hasError: false, error: null }
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`[MessageErrorBoundary] Error in message ${this.props.messageId}:`, error, info)
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="px-4 py-3">
          <div className="max-w-3xl mx-auto">
            <div className="rounded-md border border-red-500/30 bg-red-500/5 px-3 py-2 text-sm text-red-400">
              <span className="font-medium">Render error:</span>{' '}
              {this.state.error?.message || 'Unknown error'}
            </div>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}
