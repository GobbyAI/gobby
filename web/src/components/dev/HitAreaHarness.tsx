import { useState } from 'react'
import { Input } from '../ui/Input'
import { NativeSelect } from '../ui/NativeSelect'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '../ui/Select'
import { Textarea } from '../ui/Textarea'

/**
 * Dev-only surface for the coarse-pointer hit-area Playwright spec
 * (tests/coarse-pointer-hit-areas.spec.ts): the four control primitives at
 * their bare 36px ladder size, spaced far enough apart that perimeter clicks
 * can only land in one control's invisible expansion. The textarea is pinned
 * to 36px so the spec exercises its vertical expansion too.
 */
export function HitAreaHarness() {
  const [selected, setSelected] = useState('a')
  return (
    <main className="flex min-h-screen flex-col items-start gap-10 bg-background p-10">
      <Input aria-label="Harness input" data-testid="harness-input" wrapperClassName="w-64" />
      <Textarea
        aria-label="Harness textarea"
        data-testid="harness-textarea"
        rows={1}
        className="h-9 min-h-0 resize-none"
        wrapperClassName="w-64"
      />
      <NativeSelect
        aria-label="Harness native select"
        data-testid="harness-native-select"
        wrapperClassName="w-64"
      >
        <option value="a">Alpha</option>
        <option value="b">Beta</option>
      </NativeSelect>
      <Select value={selected} onValueChange={setSelected}>
        <SelectTrigger
          aria-label="Harness radix select"
          data-testid="harness-radix-trigger"
          className="w-64"
        >
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="a">Alpha</SelectItem>
          <SelectItem value="b">Beta</SelectItem>
          <SelectItem value="c">Gamma</SelectItem>
        </SelectContent>
      </Select>
    </main>
  )
}
