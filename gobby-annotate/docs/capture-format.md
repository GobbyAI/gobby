# Capture format, version 1

A bundle is a ZIP with `capture.json` and zero or more `screenshots/<name>.png`
files. An extracted directory with those exact files is equivalent. JSON is
UTF-8. PNG bytes are original native screenshots, not resized or reconstructed
from the DOM. ZIP supports stored or DEFLATE entries, not encryption, ZIP64,
multi-disk archives, symlinks, directory entries, or alternate file types.

All objects reject unknown fields. The normative executable schema is
`packages/core/src/schema.ts`. All numbers must be finite; all IDs below are UUIDs.
Timestamps are ISO 8601 UTC datetime strings. All fields listed are required.

| Manifest field | Meaning and limits                              |
| -------------- | ----------------------------------------------- |
| `version`      | Integer `1`; readers reject other versions      |
| `batchId`      | Stable draft batch UUID across exports          |
| `exportId`     | New UUID for every immutable exported snapshot  |
| `title`        | Nonempty trimmed string, at most 200 characters |
| `exportedAt`   | Export timestamp                                |
| `annotations`  | Array of 1–100 annotations with unique IDs      |

| Annotation field         | Meaning and limits                                                                                                                                         |
| ------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `id`, `revision`         | Stable annotation UUID; positive integer incremented on editing                                                                                            |
| `createdAt`, `updatedAt` | Creation and latest edit timestamps, in order                                                                                                              |
| `comment`                | Nonempty on export; maximum 20,000 characters                                                                                                              |
| `page`                   | `{url, title}`; absolute URL up to 8192 and title up to 2000 characters                                                                                    |
| `frame`                  | `{url, path, access}`; URL up to 8192 characters, locator path up to 32 strings of 2000 characters; access is `document`, `host-only`, or `visible-region` |
| `target`                 | Object described below                                                                                                                                     |
| `viewport`               | Object described below                                                                                                                                     |
| `screenshot`             | Available PNG reference or explicit unavailable reason                                                                                                     |

`target` contains `kind` (`element` or `rectangle`), `locator` (up to 64 locator
segments, each at most 2000 characters), `tag` and `role` (up to 100 characters
each), `text` (bounded semantic context up to 500 characters), `bounds`, and
`screenshotBounds`. Locator segments record selectors and accessible shadow/frame
boundaries; they are contextual hints, not executable code or guaranteed future
matches. Host-only context never claims access to hidden descendants.

Each bounds object is `{x,y,width,height}`, measured in CSS pixels, with nonnegative
width and height. `bounds` is relative to the selected frame's layout viewport;
`screenshotBounds` is relative to the top-level visual viewport's visible image.
Frame offsets and CSS scaling are applied before recording screenshot coordinates.
Pixel coordinates can be derived independently per axis using screenshot width /
visual viewport width and screenshot height / visual viewport height. Do not
assume devicePixelRatio alone accounts for zoom or browser capture scaling.

`viewport` has `layout:{width,height}`, `visual:{width,height,offsetLeft,offsetTop,scale}`,
`devicePixelRatio`, `scrollX`, and `scrollY`. Dimensions, scale, and pixel ratio are
positive. Offsets and scroll positions are CSS pixels in the top document.

Available screenshot: `{status:"available",path:"screenshots/<name>.png",width,height}`,
where name contains only ASCII letters, digits, underscore, or hyphen, and pixel
dimensions are positive integers matching PNG IHDR. Unavailable screenshot:
`{status:"unavailable",reason:"..."}` with a nonempty reason up to 2000 characters.

Readers reject missing or unreferenced assets, duplicate archive entries,
traversal/absolute paths, malformed directories, checksum or size mismatches,
overlapping ZIP records, and more than 128 MiB total uncompressed content. Encoded
ZIP overhead is bounded to a further 1 MiB. Export delivery itself is limited to
128 MiB encoded bytes. Never silently discard annotations or images to fit.

The manifest intentionally excludes task IDs, execution status, form values,
browser storage, and whole-page HTML. Task conversion belongs to the consuming
tool. Keep old exports immutable; a revision in a later export does not rewrite
an earlier snapshot. Batch/annotation IDs establish identity; export ID establishes
the exact evidence snapshot.
