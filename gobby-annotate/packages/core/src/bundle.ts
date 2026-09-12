import { Inflate, zipSync, strToU8, strFromU8 } from "fflate";
import { MAX_BYTES, manifestSchema, safePath, type Bundle } from "./schema";

// Validate names, types, and sizes before allocating decompressed output.
type ZipEntry = {
  size: number;
  compressed: number;
  start: number;
  method: number;
  crc: number;
};
export function inspectZip(bytes: Uint8Array): Map<string, ZipEntry> {
  if (bytes.byteLength > MAX_BYTES + 1024 * 1024)
    throw new Error("Bundle exceeds 128 MiB limit");
  const v = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let end = -1;
  for (let p = bytes.length - 22; p >= Math.max(0, bytes.length - 65557); p--) {
    if (
      v.getUint32(p, true) === 0x06054b50 &&
      p + 22 + v.getUint16(p + 20, true) === bytes.length
    ) {
      end = p;
      break;
    }
  }
  if (end < 0) throw new Error("Invalid ZIP: end directory missing");
  const count = v.getUint16(end + 10, true);
  const start = v.getUint32(end + 16, true);
  if (
    v.getUint16(end + 4, true) ||
    v.getUint16(end + 6, true) ||
    count !== v.getUint16(end + 8, true) ||
    count > 101 ||
    start === 0xffffffff
  )
    throw new Error("Unsupported multi-disk, ZIP64, or excessive ZIP entries");
  if (start + v.getUint32(end + 12, true) !== end)
    throw new Error("Invalid ZIP directory size");
  let p = start,
    total = 0;
  const result = new Map<string, ZipEntry>();
  const spans: [number, number][] = [];
  for (let i = 0; i < count; i++) {
    if (p + 46 > end || v.getUint32(p, true) !== 0x02014b50)
      throw new Error("Invalid ZIP directory entry");
    const size = v.getUint32(p + 24, true),
      compressed = v.getUint32(p + 20, true);
    const nl = v.getUint16(p + 28, true),
      el = v.getUint16(p + 30, true),
      cl = v.getUint16(p + 32, true);
    if (p + 46 + nl + el + cl > end) throw new Error("Truncated ZIP entry");
    const name = strFromU8(bytes.subarray(p + 46, p + 46 + nl));
    const mode = (v.getUint32(p + 38, true) >>> 16) & 0xf000;
    if (!safePath(name) || (mode && mode !== 0x8000))
      throw new Error(`Unsafe ZIP path or file type: ${name}`);
    if (result.has(name)) throw new Error(`Duplicate ZIP entry: ${name}`);
    if (
      v.getUint16(p + 8, true) & 1 ||
      ![0, 8].includes(v.getUint16(p + 10, true))
    )
      throw new Error("Encrypted or unsupported ZIP compression");
    total += size;
    if (total > MAX_BYTES)
      throw new Error("Bundle exceeds 128 MiB uncompressed limit");
    const local = v.getUint32(p + 42, true);
    if (local + 30 > start || v.getUint32(local, true) !== 0x04034b50)
      throw new Error("Invalid ZIP local header");
    const localNameLength = v.getUint16(local + 26, true);
    const dataStart =
      local + 30 + localNameLength + v.getUint16(local + 28, true);
    if (
      dataStart + compressed > start ||
      strFromU8(bytes.subarray(local + 30, local + 30 + localNameLength)) !==
        name ||
      v.getUint16(local + 8, true) !== v.getUint16(p + 10, true)
    )
      throw new Error("ZIP local header mismatch");
    spans.push([local, dataStart + compressed]);
    result.set(name, {
      size,
      compressed,
      start: dataStart,
      method: v.getUint16(p + 10, true),
      crc: v.getUint32(p + 16, true),
    });
    p += 46 + nl + el + cl;
  }
  spans.sort((a, b) => a[0] - b[0]);
  if (spans.some((span, i) => i > 0 && span[0] < spans[i - 1]![1]) || p !== end)
    throw new Error("Overlapping or invalid ZIP records");
  return result;
}

export function pngDimensions(bytes: Uint8Array): {
  width: number;
  height: number;
} {
  const signature = [137, 80, 78, 71, 13, 10, 26, 10];
  if (
    bytes.length < 33 ||
    !signature.every((n, i) => bytes[i] === n) ||
    strFromU8(bytes.subarray(12, 16)) !== "IHDR"
  )
    throw new Error("Screenshot is not a PNG");
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const width = view.getUint32(16),
    height = view.getUint32(20);
  if (!width || !height || width > 65535 || height > 65535)
    throw new Error("Invalid PNG dimensions");
  let imageData = false;
  let dataEnded = false;
  for (let offset = 8; offset < bytes.length;) {
    if (offset + 12 > bytes.length) throw new Error("Truncated PNG chunk");
    const length = view.getUint32(offset);
    const end = offset + 12 + length;
    if (end > bytes.length) throw new Error("Truncated PNG chunk data");
    const type = strFromU8(bytes.subarray(offset + 4, offset + 8));
    if (!/^[A-Za-z]{4}$/.test(type)) throw new Error("Invalid PNG chunk type");
    if (crc32(bytes.subarray(offset + 4, end - 4)) !== view.getUint32(end - 4))
      throw new Error(`PNG checksum mismatch: ${type}`);
    if (type === "IHDR") {
      if (offset !== 8 || length !== 13) throw new Error("Invalid PNG header");
      const depths: Record<number, number[]> = {
        0: [1, 2, 4, 8, 16],
        2: [8, 16],
        3: [1, 2, 4, 8],
        4: [8, 16],
        6: [8, 16],
      };
      if (
        !depths[bytes[25]]?.includes(bytes[24]) ||
        bytes[26] !== 0 ||
        bytes[27] !== 0 ||
        bytes[28] > 1
      )
        throw new Error("Unsupported PNG header fields");
    } else if (type === "IDAT") {
      if (dataEnded) throw new Error("Nonconsecutive PNG image data");
      imageData = true;
    } else {
      if (imageData) dataEnded = true;
      if (type === "IEND") {
        if (!imageData || length !== 0 || end !== bytes.length)
          throw new Error("Invalid PNG trailer");
        return { width, height };
      }
      if (type[0] === type[0].toUpperCase() && type !== "PLTE")
        throw new Error(`Unsupported critical PNG chunk: ${type}`);
    }
    offset = end;
  }
  throw new Error("Missing PNG trailer");
}

export function decodeEntries(entries: Map<string, Uint8Array>): Bundle {
  let size = 0;
  for (const [name, bytes] of entries) {
    if (!safePath(name)) throw new Error(`Unsafe bundle path: ${name}`);
    size += bytes.byteLength;
  }
  if (size > MAX_BYTES)
    throw new Error("Bundle exceeds 128 MiB uncompressed limit");
  const json = entries.get("capture.json");
  if (!json) throw new Error("Missing capture.json");
  if (json.byteLength > 8 * 1024 * 1024)
    throw new Error("capture.json exceeds 8 MiB");
  const manifest = manifestSchema.parse(JSON.parse(strFromU8(json)));
  const assets = new Map<string, Uint8Array>();
  for (const a of manifest.annotations) {
    if (a.screenshot.status !== "available") continue;
    const bytes = entries.get(a.screenshot.path);
    if (!bytes) throw new Error(`Missing screenshot: ${a.screenshot.path}`);
    const dimensions = pngDimensions(bytes);
    if (
      dimensions.width !== a.screenshot.width ||
      dimensions.height !== a.screenshot.height
    )
      throw new Error(`Screenshot dimension mismatch: ${a.id}`);
    assets.set(a.screenshot.path, bytes);
  }
  if (entries.size !== assets.size + 1)
    throw new Error("Bundle contains unreferenced files");
  return { manifest, assets };
}

export function decodeBundle(bytes: Uint8Array): Bundle {
  const inventory = inspectZip(bytes);
  const files = new Map<string, Uint8Array>();
  for (const [name, entry] of inventory) {
    const output = new Uint8Array(entry.size);
    let length = 0;
    const append = (chunk: Uint8Array) => {
      if (length + chunk.length > entry.size)
        throw new Error(`ZIP size mismatch: ${name}`);
      output.set(chunk, length);
      length += chunk.length;
    };
    const input = bytes.subarray(entry.start, entry.start + entry.compressed);
    if (entry.method === 0) append(input);
    else {
      const inflater = new Inflate(append);
      // Bound expansion per callback even when the declared size is forged.
      for (let offset = 0; offset < input.length; offset += 256) {
        inflater.push(
          input.subarray(offset, offset + 256),
          offset + 256 >= input.length,
        );
      }
      if (!input.length) inflater.push(input, true);
    }
    if (length !== entry.size) throw new Error(`ZIP size mismatch: ${name}`);
    if (crc32(output) !== entry.crc)
      throw new Error(`ZIP checksum mismatch: ${name}`);
    files.set(name, output);
  }
  return decodeEntries(files);
}

const crcTable = Uint32Array.from({ length: 256 }, (_, value) => {
  for (let bit = 0; bit < 8; bit++)
    value = (value >>> 1) ^ (value & 1 ? 0xedb88320 : 0);
  return value >>> 0;
});
function crc32(bytes: Uint8Array): number {
  let crc = 0xffffffff;
  for (const byte of bytes) crc = (crc >>> 8) ^ crcTable[(crc ^ byte) & 255]!;
  return (crc ^ 0xffffffff) >>> 0;
}

export function encodeBundle(bundle: Bundle): Uint8Array {
  const entries = new Map(bundle.assets);
  entries.set("capture.json", strToU8(JSON.stringify(bundle.manifest)));
  decodeEntries(entries);
  return zipSync(Object.fromEntries(entries), { level: 0 });
}
