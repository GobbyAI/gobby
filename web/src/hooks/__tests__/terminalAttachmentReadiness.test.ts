import { describe, expect, it } from "vitest";

import { createAttachmentReadiness } from "../terminalAttachmentReadiness";

describe("terminalAttachmentReadiness", () => {
  it("defers viewport until ready", () => {
    const readiness = createAttachmentReadiness();

    // Neither fact alone is readiness. A viewport frame without an attachment
    // id is dropped on the floor by the daemon, and one without real
    // dimensions has nothing to say.
    expect(readiness.take()).toBeNull();
    readiness.size(24, 80);
    expect(readiness.take()).toBeNull();

    readiness.attach("att-1", "t1");
    expect(readiness.take()).toEqual({
      attachmentId: "att-1",
      terminalId: "t1",
      rows: 24,
      cols: 80,
    });

    // Once, not once per render: the rendezvous is consumed.
    expect(readiness.take()).toBeNull();

    // The other arrival order is the common one — attach resolves before the
    // renderer has measured itself.
    const reordered = createAttachmentReadiness();
    reordered.attach("att-2", "t2");
    expect(reordered.take()).toBeNull();
    reordered.size(40, 120);
    expect(reordered.take()).toEqual({
      attachmentId: "att-2",
      terminalId: "t2",
      rows: 40,
      cols: 120,
    });

    // A replacement attachment re-arms: the new one has never been told the
    // viewport, and the measured size carries over because the pane did not
    // move.
    reordered.attach("att-3", "t2");
    expect(reordered.take()).toEqual({
      attachmentId: "att-3",
      terminalId: "t2",
      rows: 40,
      cols: 120,
    });

    // Degenerate sizes are not a viewport. A zero-row grid is what a renderer
    // reports while it is still laying out.
    const unmeasured = createAttachmentReadiness();
    unmeasured.attach("att-4", "t4");
    unmeasured.size(0, 80);
    expect(unmeasured.take()).toBeNull();
    unmeasured.size(24, 80);
    expect(unmeasured.take()).toMatchObject({ attachmentId: "att-4" });

    // Detaching drops the pending rendezvous rather than letting it fire at
    // the next attachment.
    const detached = createAttachmentReadiness();
    detached.size(24, 80);
    detached.attach("att-5", "t5");
    detached.reset();
    expect(detached.take()).toBeNull();
  });
});
