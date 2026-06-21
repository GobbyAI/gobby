type ChatLogContext = Record<string, unknown>;

function serializeError(value: Error, seen: WeakSet<object>): ChatLogContext {
  const serialized: ChatLogContext = {
    name: value.name,
    message: value.message,
  };
  if (value.stack) {
    serialized.stack = value.stack;
  }
  if ("cause" in value) {
    serialized.cause = serializeValue(value.cause, seen);
  }
  return serialized;
}

function serializeDomLike(value: object): ChatLogContext | null {
  const record = value as Record<string, unknown>;
  if (typeof record.nodeName !== "string" && typeof record.tagName !== "string") {
    return null;
  }
  return {
    nodeName: record.nodeName ?? record.tagName,
    id: typeof record.id === "string" && record.id ? record.id : null,
    className:
      typeof record.className === "string" && record.className
        ? record.className
        : null,
  };
}

function serializeValue(value: unknown, seen: WeakSet<object>): unknown {
  if (value == null) return value;
  const valueType = typeof value;
  if (valueType === "string" || valueType === "number" || valueType === "boolean") {
    return value;
  }
  if (valueType === "bigint") {
    return value.toString();
  }
  if (valueType === "function") {
    const fn = value as (...args: unknown[]) => unknown;
    return `[Function ${fn.name || "anonymous"}]`;
  }
  if (valueType !== "object") {
    return String(value);
  }

  if (seen.has(value)) {
    return "[Circular]";
  }
  seen.add(value);

  if (value instanceof Error) {
    return serializeError(value, seen);
  }
  if (value instanceof Date) {
    return Number.isNaN(value.valueOf()) ? String(value) : value.toISOString();
  }
  if (Array.isArray(value)) {
    return value.map((item) => serializeValue(item, seen));
  }
  if (value instanceof Map) {
    return Object.fromEntries(
      Array.from(value.entries()).map(([key, entryValue]) => [
        String(key),
        serializeValue(entryValue, seen),
      ]),
    );
  }
  if (value instanceof Set) {
    return Array.from(value.values()).map((item) => serializeValue(item, seen));
  }

  const domLike = serializeDomLike(value);
  if (domLike) {
    return domLike;
  }

  try {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([key, entryValue]) => [
        key,
        serializeValue(entryValue, seen),
      ]),
    );
  } catch (error) {
    const tag = Object.prototype.toString.call(value);
    return `[Unserializable ${tag}: ${String(error)}]`;
  }
}

function serializeContext(context: ChatLogContext | undefined): ChatLogContext | undefined {
  if (!context) return undefined;
  const seen = new WeakSet<object>();
  return Object.fromEntries(
    Object.entries(context).map(([key, value]) => [
      key,
      serializeValue(value, seen),
    ]),
  );
}

export const chatLogger = {
  debug(message: string, context?: ChatLogContext): void {
    console.debug(message, serializeContext(context));
  },
  error(message: string, context?: ChatLogContext): void {
    console.error(message, serializeContext(context));
  },
  warn(message: string, context?: ChatLogContext): void {
    console.warn(message, serializeContext(context));
  },
};
