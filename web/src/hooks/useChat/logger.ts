type ChatLogContext = Record<string, unknown>;

function serializeError(value: unknown): unknown {
  if (value instanceof Error) {
    return {
      name: value.name,
      message: value.message,
      stack: value.stack,
    };
  }
  return value;
}

function serializeContext(context: ChatLogContext | undefined): ChatLogContext | undefined {
  if (!context) return undefined;
  return Object.fromEntries(
    Object.entries(context).map(([key, value]) => [
      key,
      key === "error" ? serializeError(value) : value,
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
