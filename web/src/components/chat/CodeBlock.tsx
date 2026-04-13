import type { Components } from 'react-markdown'
import {
  Anchor,
  CodeBlockInner,
  ImageBlock,
  TableWrapper,
} from './CodeBlockRenderers'

export const codeBlockComponents: Partial<Components> = {
  code: CodeBlockInner as Components['code'],
  table: TableWrapper as Components['table'],
  a: Anchor as Components['a'],
  img: ImageBlock as Components['img'],
}
