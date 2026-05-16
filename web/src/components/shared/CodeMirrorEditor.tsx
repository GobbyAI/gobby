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
import { CODE_CHROME_TYPOGRAPHY, CODE_CHROME_VARS } from './codeBlockTheme'
import { useResolvedTheme } from '../../hooks/useResolvedTheme'
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
  const resolvedTheme = useResolvedTheme()
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
      // Light mode uses CodeMirror's built-in light-appropriate
      // defaultHighlightStyle; dark mode keeps One Dark over the default
      // fallback. One Dark on a light background is unreadable.
      ...(resolvedTheme === 'dark'
        ? [
            syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
            syntaxHighlighting(oneDarkHighlightStyle),
          ]
        : [syntaxHighlighting(defaultHighlightStyle)]),
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
          fontSize: CODE_CHROME_TYPOGRAPHY.fontSize,
          backgroundColor: CODE_CHROME_VARS.bg,
          borderRadius: CODE_CHROME_TYPOGRAPHY.borderRadius,
        },
        '.cm-scroller': {
          fontFamily: CODE_CHROME_TYPOGRAPHY.fontFamily,
          overflow: 'auto',
        },
        '.cm-content': {
          paddingTop: CODE_CHROME_TYPOGRAPHY.padding,
          paddingBottom: CODE_CHROME_TYPOGRAPHY.padding,
        },
        '.cm-gutters': {
          backgroundColor: CODE_CHROME_VARS.bg,
          borderRight: `1px solid ${CODE_CHROME_VARS.gutterBorder}`,
          color: CODE_CHROME_VARS.gutterText,
          paddingTop: CODE_CHROME_TYPOGRAPHY.padding,
          paddingBottom: CODE_CHROME_TYPOGRAPHY.padding,
        },
        '.cm-activeLineGutter': {
          background: CODE_CHROME_VARS.activeLineBg,
        },
        '.cm-activeLine': {
          background: CODE_CHROME_VARS.activeLineBg,
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
  }, [language, readOnly, handleSave, resolvedTheme]) // Recreate on language/readOnly/theme change

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

  return <div ref={containerRef} className="codemirror-container h-full overflow-hidden [&_.cm-editor]:h-full" />
}
