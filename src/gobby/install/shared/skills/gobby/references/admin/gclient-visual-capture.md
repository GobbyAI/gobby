# gclient visual capture

Load when an agent must see the live gclient window as pixels: visual QA of
the running client or reproducing a layout report from the user. `capture-pane`
and `read_pane` return one pane's text; they cannot show chrome, colors,
dividers, or how panes sit together. For a candidate build, use the isolated
tmux preview recipe in memory `06aaaa63` instead; this reference captures only
the user's live window.

## Preconditions

Capture needs Screen Recording for the host process tree and an unlocked
screen. Claude seats in daemon-owned gclient panes (process tree rooted at
`~/.gobby/bin/gterm`) have captured successfully. Never change Screen Recording
grants yourself.

Check the lock first:

```bash
osascript -l JavaScript -e 'ObjC.import("CoreGraphics"); const s = ObjC.deepUnwrap(ObjC.castRefToObject($.CGSessionCopyCurrentDictionary())); s.CGSSessionScreenIsLocked ? "locked" : "unlocked"'
```

If it prints `locked`, stop and report "screen locked". A locked screen makes
`-l` fail with `could not create image from window` and turns full-screen
captures solid black, which looks like a permission failure but is not.

Codex and other sandboxed seats may be refused even when unlocked. A seat that
cannot capture sends the request, with what to look at, to a seat that can
through `gobby-agents:send_message`; the Assistant is the usual one. The
capturing seat reports what it saw in text and never sends the image.

## Capture the window only

Never capture the whole screen. Look up the Ghostty window id with CoreGraphics
through JXA (no toolchain needed):

```bash
osascript -l JavaScript -e 'ObjC.import("CoreGraphics"); const ws = ObjC.deepUnwrap(ObjC.castRefToObject($.CGWindowListCopyWindowInfo($.kCGWindowListOptionOnScreenOnly | $.kCGWindowListExcludeDesktopElements, 0))); ws.filter(w => w.kCGWindowOwnerName === "Ghostty" && w.kCGWindowLayer === 0).map(w => { const b = w.kCGWindowBounds; return [w.kCGWindowNumber, w.kCGWindowName || "", [b.X, b.Y, b.Width, b.Height].join(",")].join("\t"); }).join("\n")'
```

Each line is `id<TAB>title<TAB>x,y,width,height`. Pick the window titled
`gclient`. If several match, ask rather than guess. Then capture it without
sound (`-x`) or window shadow (`-o`):

```bash
screencapture -x -o -l <windowid> <scratchpad>/gclient-window.png
```

The image is the whole host Ghostty window at Retina scale: Ghostty's title bar
and its tab strip, whose titles name the user's other tabs, sit above the
gclient UI. Judge only the gclient area. View the file with the Read tool.

## Handling the image

- Write only to the session scratchpad directory. Never write into the
  repository or a worktree, and never attach, upload, or publish the image.
- Delete the file as soon as you have read it, even when the capture looked
  wrong.
- The window shows whatever is on the user's screen in gclient, including other
  sessions' panes. Use it only for gclient QA. Report findings in text and do not
  transcribe unrelated pane content.

For gclient controls and layout, see the
[gclient user guide](../../../../../../../../docs/guides/gclient-user-guide.md).
