import { describe, expect, it } from 'vitest'

import { contrastRatio } from '../../../lib/colorContrast'
import { CODE_CHROME } from '../codeBlockTheme'

/**
 * Regression gate for #17153. The CodeMirror gutter line numbers
 * (`--code-gutter-text`, the only *text* token in CODE_CHROME) must clear the
 * WCAG 2.2 AA 4.5:1 floor against every code surface they can sit on, in both
 * themes. Reverting the token to its prior sub-AA values must fail this test.
 *
 * Surfaces (`bg`, `bgBlock`) and dividers/fills (`gutterBorder`,
 * `activeLineBg`) are not text and are intentionally not asserted here.
 */
const AA_NORMAL_TEXT = 4.5
const THEMES = ['dark', 'light'] as const

describe('code-chrome gutter text meets WCAG 2.2 AA', () => {
  for (const theme of THEMES) {
    it(`${theme}: gutter line numbers clear 4.5:1 on both code surfaces`, () => {
      const gutter = CODE_CHROME.gutterText[theme]
      expect(contrastRatio(gutter, CODE_CHROME.bg[theme])).toBeGreaterThanOrEqual(AA_NORMAL_TEXT)
      expect(contrastRatio(gutter, CODE_CHROME.bgBlock[theme])).toBeGreaterThanOrEqual(AA_NORMAL_TEXT)
    })
  }
})
