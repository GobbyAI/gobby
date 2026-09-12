import { constants } from "node:fs";
import { lstat, open, readdir, realpath } from "node:fs/promises";
import { join, relative, resolve, sep } from "node:path";
import {
  decodeBundle,
  decodeEntries,
  MAX_BYTES,
  safePath,
  type Bundle,
} from "@gobby/annotate-core";

export async function readBounded(
  path: string,
  limit = MAX_BYTES,
): Promise<Uint8Array> {
  const file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const stat = await file.stat();
    if (!stat.isFile() || stat.size > limit)
      throw new Error(`Not a regular file within size limit: ${path}`);
    const bytes = new Uint8Array(stat.size);
    let offset = 0;
    while (offset < bytes.length) {
      const { bytesRead } = await file.read(
        bytes,
        offset,
        bytes.length - offset,
        offset,
      );
      if (!bytesRead) throw new Error(`File changed during read: ${path}`);
      offset += bytesRead;
    }
    const after = await file.stat();
    if (after.size !== stat.size || after.mtimeMs !== stat.mtimeMs)
      throw new Error(`File changed during read: ${path}`);
    return bytes;
  } finally {
    await file.close();
  }
}

export async function readCapture(path: string): Promise<Bundle> {
  const stat = await lstat(path);
  if (stat.isSymbolicLink())
    throw new Error(`Symlinks are not supported: ${path}`);
  if (stat.isFile())
    return decodeBundle(await readBounded(path, MAX_BYTES + 1024 * 1024));
  if (!stat.isDirectory())
    throw new Error(`Expected ZIP or extracted capture directory: ${path}`);
  const entries = new Map<string, Uint8Array>();
  let total = 0;
  async function walk(dir: string, prefix: string): Promise<void> {
    const listing = await readdir(dir, { withFileTypes: true });
    if (listing.length > 101) throw new Error("Too many capture files");
    for (const item of listing) {
      const name = prefix + item.name;
      if (item.isSymbolicLink()) throw new Error(`Symlink in capture: ${name}`);
      if (item.isDirectory() && name === "screenshots") {
        await walk(join(dir, item.name), "screenshots/");
      } else {
        if (!item.isFile() || !safePath(name))
          throw new Error(`Unsafe capture entry: ${name}`);
        const bytes = await readBounded(
          join(dir, item.name),
          MAX_BYTES - total,
        );
        total += bytes.length;
        entries.set(name, bytes);
      }
    }
  }
  await walk(path, "");
  return decodeEntries(entries);
}

export class Catalog {
  readonly root: string;
  constructor(root: string) {
    this.root = resolve(root);
  }

  async validateRoot(): Promise<void> {
    const stat = await lstat(this.root).catch(() => {
      throw new Error(
        `Capture root does not exist: ${this.root}. Create it and pass --root <directory>.`,
      );
    });
    if (!stat.isDirectory() || stat.isSymbolicLink())
      throw new Error("Capture root must be a real directory");
  }

  async discover(): Promise<{
    captures: {
      exportId: string;
      batchId: string;
      title: string;
      exportedAt: string;
      count: number;
      location: string;
    }[];
    errors: { location: string; error: string }[];
  }> {
    await this.validateRoot();
    const listing = await readdir(this.root, { withFileTypes: true });
    if (listing.length > 1000)
      throw new Error(
        "Capture root exceeds 1000 entries; use a smaller directory",
      );
    const captures = [],
      errors = [];
    const seen = new Set<string>();
    for (const entry of listing.sort((a, b) => a.name.localeCompare(b.name))) {
      if (!entry.isDirectory() && !entry.name.endsWith(".zip")) continue;
      try {
        const path = await this.contained(entry.name);
        const { manifest: m } = await readCapture(path);
        if (seen.has(m.exportId))
          throw new Error(
            `Duplicate export ID: ${m.exportId}; keep one immutable export per ID`,
          );
        seen.add(m.exportId);
        captures.push({
          exportId: m.exportId,
          batchId: m.batchId,
          title: m.title,
          exportedAt: m.exportedAt,
          count: m.annotations.length,
          location: entry.name,
        });
      } catch (error) {
        errors.push({
          location: entry.name,
          error: error instanceof Error ? error.message : String(error),
        });
      }
    }
    return { captures, errors };
  }

  async contained(name: string): Promise<string> {
    const path = resolve(this.root, name),
      rel = relative(this.root, path);
    if (!rel || rel.startsWith(".." + sep) || rel === "..")
      throw new Error("Capture path escapes configured root");
    const actual = await realpath(path);
    if (
      actual !== resolve(await realpath(this.root), rel) ||
      (await lstat(path)).isSymbolicLink()
    )
      throw new Error("Symlink paths are not supported");
    return path;
  }

  async get(exportId: string): Promise<Bundle> {
    const inventory = await this.discover();
    if (inventory.errors.some((e) => e.error.includes(exportId)))
      throw new Error(`Conflicting or invalid export: ${exportId}`);
    const capture = inventory.captures.find((c) => c.exportId === exportId);
    if (!capture)
      throw new Error(
        `Export not found: ${exportId}. Use list_captures and inspect its errors.`,
      );
    const bundle = await readCapture(await this.contained(capture.location));
    if (bundle.manifest.exportId !== exportId)
      throw new Error(
        `Capture changed during read: ${exportId}. Retry discovery.`,
      );
    return bundle;
  }
}
