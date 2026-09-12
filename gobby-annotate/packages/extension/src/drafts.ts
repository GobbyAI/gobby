import {
  annotationSchema,
  MAX_ANNOTATIONS,
  MAX_BYTES,
  type Annotation,
  type Bundle,
} from "@gobby/annotate-core";

export type Batch = {
  id: string;
  title: string;
  updatedAt: string;
  annotations: Annotation[];
};
const request = <T>(req: IDBRequest<T>) =>
  new Promise<T>((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
const completed = (tx: IDBTransaction) =>
  new Promise<void>((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = tx.onabort = () =>
      reject(tx.error ?? new Error("Draft transaction aborted"));
  });

export class Drafts {
  private db: Promise<IDBDatabase>;
  constructor(name = "gobby-annotate") {
    this.db = new Promise((resolve, reject) => {
      const req = indexedDB.open(name, 1);
      req.onupgradeneeded = () => {
        req.result.createObjectStore("batches", { keyPath: "id" });
        req.result.createObjectStore("images");
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }
  async list(): Promise<Batch[]> {
    const tx = (await this.db).transaction("batches", "readonly");
    return request<Batch[]>(tx.objectStore("batches").getAll());
  }
  async create(title = "Untitled batch"): Promise<Batch> {
    const batch: Batch = {
      id: crypto.randomUUID(),
      title,
      updatedAt: new Date().toISOString(),
      annotations: [],
    };
    const tx = (await this.db).transaction("batches", "readwrite");
    const done = completed(tx);
    tx.objectStore("batches").add(batch);
    await done;
    return batch;
  }
  async change(
    batchId: string,
    operation:
      | { title: string }
      | { annotation: Annotation; image?: Blob }
      | { deleteId: string },
  ): Promise<Batch> {
    const tx = (await this.db).transaction(["batches", "images"], "readwrite");
    const done = completed(tx);
    void done.catch(() => {}); // The same rejection is awaited below; avoid an early unhandled event.
    try {
      const batches = tx.objectStore("batches"),
        images = tx.objectStore("images");
      const batch: Batch | undefined = await request(batches.get(batchId));
      if (!batch) throw new Error("Batch no longer exists");
      if ("title" in operation) {
        const title = operation.title.trim();
        if (!title || title.length > 200)
          throw new Error("Batch title must contain 1–200 characters");
        batch.title = title;
      } else if ("deleteId" in operation) {
        const old = batch.annotations.find((a) => a.id === operation.deleteId);
        if (old?.screenshot.status === "available")
          images.delete(batchId + "/" + old.screenshot.path);
        batch.annotations = batch.annotations.filter(
          (a) => a.id !== operation.deleteId,
        );
      } else {
        const a = annotationSchema.parse(operation.annotation);
        const index = batch.annotations.findIndex((item) => item.id === a.id),
          old = batch.annotations[index];
        if (old && a.revision !== old.revision)
          throw new Error(
            "Draft changed in another tab. Reopen it before editing.",
          );
        if (!old && batch.annotations.length >= MAX_ANNOTATIONS)
          throw new Error("Batch limit is 100 annotations");
        if (operation.image && operation.image.size > MAX_BYTES)
          throw new Error("Screenshot exceeds 128 MiB");
        a.revision = old ? old.revision + 1 : 1;
        a.createdAt = old?.createdAt ?? a.createdAt;
        a.updatedAt = new Date().toISOString();
        if (a.screenshot.status === "available") {
          const key = batchId + "/" + a.screenshot.path;
          if (operation.image) images.put(operation.image, key);
          else if (!(await request(images.get(key))))
            throw new Error("Screenshot data missing; retry capture");
        }
        if (
          old?.screenshot.status === "available" &&
          (a.screenshot.status !== "available" ||
            a.screenshot.path !== old.screenshot.path)
        )
          images.delete(batchId + "/" + old.screenshot.path);
        if (old) batch.annotations[index] = a;
        else batch.annotations.push(a);
      }
      batch.updatedAt = new Date().toISOString();
      batches.put(batch);
      await done;
      return batch;
    } catch (error) {
      try {
        tx.abort();
      } catch {
        /* Already completed or aborted. */
      }
      throw error;
    }
  }
  async snapshot(batchId: string): Promise<Bundle> {
    const tx = (await this.db).transaction(["batches", "images"], "readonly");
    const done = completed(tx);
    const batch: Batch | undefined = await request(
      tx.objectStore("batches").get(batchId),
    );
    if (!batch) throw new Error("Batch not found");
    const images: [string, Blob][] = [];
    for (const a of batch.annotations)
      if (a.screenshot.status === "available") {
        const blob: Blob | undefined = await request(
          tx.objectStore("images").get(batchId + "/" + a.screenshot.path),
        );
        if (!blob) throw new Error(`Screenshot missing for ${a.id}`);
        images.push([a.screenshot.path, blob]);
      }
    await done;
    const assets = new Map<string, Uint8Array>();
    let size = 0;
    for (const [path, blob] of images) {
      size += blob.size;
      if (size > MAX_BYTES) throw new Error("Batch exceeds 128 MiB");
      assets.set(path, new Uint8Array(await blob.arrayBuffer()));
    }
    return {
      manifest: {
        version: 1,
        batchId,
        exportId: crypto.randomUUID(),
        exportedAt: new Date().toISOString(),
        title: batch.title,
        annotations: batch.annotations,
      },
      assets,
    };
  }
  async image(batchId: string, path: string): Promise<Blob | undefined> {
    return request(
      (await this.db)
        .transaction("images")
        .objectStore("images")
        .get(batchId + "/" + path),
    );
  }
}
