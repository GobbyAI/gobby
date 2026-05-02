import { useRef, useEffect, useCallback } from 'react'
import { EditorView, keymap, lineNumbers, highlightActiveLine, highlightActiveLineGutter } from '@codemirror/view'
import { EditorState } from '@codemirror/state'
import { defaultKeymap, history, historyKeymap, indentWithTab } from '@codemirror/commands'
import { syntaxHighlighting, defaultHighlightStyle, bracketMatching, indentOnInput } from '@codemirror/language'
import { searchKeymap, highlightSelectionMatches } from '@codemirror/search'
import { oneDarkHighlightStyle } from '@codemirror/theme-one-dark'
import { javascript } from '@codemirror/lang-javascript'
import { python } from '@codemirror/lang-python'
import { json } from '@codemirror/lang-json'
import { css } from '@codemirror/lang-css'
import { html } from '@codemirror/lang-html'
import { markdown } from '@codemirror/lang-markdown'
import { yaml } from '@codemirror/lang-yaml'
import { StreamLanguage } from '@codemirror/language'
import { shell } from '@codemirror/legacy-modes/mode/shell'
import { toml } from '@codemirror/legacy-modes/mode/toml'

interface CodeMirrorEditorProps {
  content: string
  language: string
  readOnly?: boolean
  onChange?: (content: string) => void
  onSave?: () => void
  editorViewRef?: React.MutableRefObject<EditorView | null>
}

function getLanguageExtension(lang: string) {
  switch (lang) {
    case 'javascript':
    case 'jsx':
      return javascript({ jsx: true })
    case 'typescript':
    case 'tsx':
      return javascript({ jsx: true, typescript: true })
    case 'python':
      return python()
    case 'json':
      return json()
    case 'css':
    case 'scss':
    case 'less':
      return css()
    case 'html':
    case 'xml':
      return html()
    case 'markdown':
      return markdown()
    case 'yaml':
    case 'yml':
      return yaml()
    case 'bash':
    case 'sh':
    case 'zsh':
      return StreamLanguage.define(shell)
    case 'toml':
      return StreamLanguage.define(toml)
    default:
      return null
  }
}

export function CodeMirrorEditor({ content, language, readOnly = false, onChange, onSave, editorViewRef }: CodeMirrorEditorProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const viewRef = useRef<EditorView | null>(null)
  const onChangeRef = useRef(onChange)
  const onSaveRef = useRef(onSave)
  const contentRef = useRef(content)
  const editorViewRefRef = useRef(editorViewRef)

  useEffect(() => {
    onChangeRef.current = onChange
  }, [onChange])

  useEffect(() => {
    onSaveRef.current = onSave
  }, [onSave])

  useEffect(() => {
    contentRef.current = content
  }, [content])

  useEffect(() => {
    editorViewRefRef.current = editorViewRef
  }, [editorViewRef])

  const handleSave = useCallback(() => {
    onSaveRef.current?.()
    return true
  }, [])

  useEffect(() => {
    if (!containerRef.current) return

    const langExt = getLanguageExtension(language)

    const extensions = [
      lineNumbers(),
      highlightActiveLine(),
      highlightActiveLineGutter(),
      history(),
      bracketMatching(),
      indentOnInput(),
      highlightSelectionMatches(),
      syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
      syntaxHighlighting(oneDarkHighlightStyle),
      keymap.of([
        ...defaultKeymap,
        ...historyKeymap,
        ...searchKeymap,
        indentWithTab,
        { key: 'Mod-s', run: () => handleSave() },
      ]),
      EditorView.updateListener.of(update => {
        if (update.docChanged) {
          onChangeRef.current?.(update.state.doc.toString())
        }
      }),
      EditorView.theme({
        '&': {
          height: '100%',
          fontSize: '14px',
          backgroundColor: '#0a0a0a',
        },
        '.cm-scroller': {
          fontFamily: "'SF Mono', 'Fira Code', 'JetBrains Mono', monospace",
          overflow: 'auto',
        },
        '.cm-gutters': {
          backgroundColor: '#0a0a0a',
          borderRight: '1px solid #262626',
          color: '#555',
        },
        '.cm-activeLineGutter': {
          background: '#1a1a1a',
        },
        '.cm-activeLine': {
          background: 'rgba(255, 255, 255, 0.03)',
        },
      }),
    ]

    if (langExt) {
      extensions.push(langExt)
    }

    if (readOnly) {
      extensions.push(EditorState.readOnly.of(true))
      extensions.push(EditorView.editable.of(false))
    }

    const state = EditorState.create({
      doc: contentRef.current,
      extensions,
    })

    const view = new EditorView({
      state,
      parent: containerRef.current,
    })

    viewRef.current = view
    if (editorViewRefRef.current) editorViewRefRef.current.current = view

    return () => {
      view.destroy()
      viewRef.current = null
      if (editorViewRefRef.current) editorViewRefRef.current.current = null
    }
  }, [language, readOnly, handleSave]) // Recreate on language/readOnly change

  // Update content when it changes externally (e.g., file reload)
  useEffect(() => {
    const view = viewRef.current
    if (!view) return
    const currentContent = view.state.doc.toString()
    if (currentContent !== content) {
      view.dispatch({
        changes: { from: 0, to: currentContent.length, insert: content },
      })
    }
  }, [content])

  return <div ref={containerRef} className="codemirror-container h-full overflow-hidden [&_.cm-editor]:h-full" style={{ background: '#0a0a0a', height: '100%' }} />
}
