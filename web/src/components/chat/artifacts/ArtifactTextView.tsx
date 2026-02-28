import { Markdown } from '../Markdown'
import { CodeMirrorEditor } from '../../shared/CodeMirrorEditor'

interface ArtifactTextViewProps {
  content: string
  artifactId: string
  isEditing?: boolean
  showSource?: boolean
  onChange?: (content: string) => void
}

export function ArtifactTextView({ content, artifactId, isEditing = false, showSource = false, onChange }: ArtifactTextViewProps) {
  return (
    <div className="flex flex-col h-full">
      <div className="flex-1 min-h-0 overflow-auto">
        {onChange && isEditing ? (
          <CodeMirrorEditor
            content={content}
            language="markdown"
            readOnly={false}
            onChange={onChange}
          />
        ) : showSource && !onChange ? (
          <pre className="text-sm font-mono text-foreground whitespace-pre-wrap p-4">{content}</pre>
        ) : (
          <div className="prose dark:prose-invert max-w-none p-4 text-sm leading-relaxed prose-p:text-sm prose-p:leading-relaxed prose-headings:text-base prose-li:text-sm prose-code:text-xs">
            <Markdown content={content} id={`artifact-text-${artifactId}`} />
          </div>
        )}
      </div>
    </div>
  )
}
