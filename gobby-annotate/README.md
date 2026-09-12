# Gobby Annotate

Select page elements or visible regions, write comments, and explicitly export a
portable ZIP containing JSON and native PNG screenshots. Drafts stay in browser
storage. No account, Gobby daemon, cloud service, telemetry, or synchronization is
required to annotate or export.

Development builds and automated checks are available. Signed Safari and physical
device acceptance remain pending; see the [verification record](docs/verification.md).

## Build and install

Use Node 22.18 or newer and npm. From this directory:

```sh
npm ci
npm run build
npm run typecheck
npm test
```

In desktop Chrome, open `chrome://extensions`, enable Developer mode, choose
**Load unpacked**, and select `packages/extension/dist/chrome`. Pin Gobby Annotate
and click its toolbar action on the page you want to annotate. Activation is
user-triggered and requests access only to that tab. Browser-internal pages and
some protected sites cannot be annotated or captured.

For desktop Safari and Safari on iPhone/iPad, follow [Safari setup](docs/safari.md).
Mobile Chrome is unsupported. Safari uses the device's actual viewport; the
extension contains no viewport simulator or iframe workbench.

## Annotate and export

Choose element or rectangle selection. Select the target, then write a comment
after screenshot capture. Escape or Cancel returns to browsing. Open the count
control to review, edit, or delete notes; the menu names/reopens batches and
exports them. Collapse the toolbar when it is in the way. Drafts autosave locally
and remain after export; an error message means the change has not been saved.

Native screenshots preserve the visible page's pixels. They may include private
information visible on that page. Inspect the screenshot before sharing. Form
values, storage, and whole-page HTML are not collected as structured context.
Open shadow roots and accessible same-origin frames supply context; inaccessible
frames and closed shadow roots permit visible-region or host selection only.

If capture fails, retry or explicitly save without a screenshot. If the page
moves or navigates during capture, select again. Storage failures require freeing
space or repairing browser storage before retrying. Export requires at least one
annotation and a nonempty comment on every note. Limits are 100 notes and 128 MiB
of uncompressed content; exceeding limits reports an error without truncation.

Chrome asks where to save the ZIP. Cancelling the save leaves drafts intact.
Safari stages the completed ZIP in its containing app, where Save or Share sends
it to Files, another app, or the coding computer. Staging does not transfer it to
your computer automatically. Interrupted exports remain recoverable from drafts.

Read exports with any compatible tool, the [standalone MCP/CLI](docs/mcp.md), or
[/gobby annotate](docs/gobby-integration.md). The [capture contract](docs/capture-format.md)
documents the portable format. Development installation is the release target;
store submission is outside this project.

Gobby branding and FSL-1.1-ALv2 licensing are inherited from the repository.
Distributions include its LICENSE. This implementation is independent of
Drawbridge; `/gobby bridge` remains the Drawbridge integration.
