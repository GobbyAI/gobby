import type { Logger } from "vite";

const EXPECTED_SOCKET_TEARDOWN_CODES = new Set(["ECONNRESET", "EPIPE"]);

export function proxyAwareErrorLogger(
  logger: Pick<Logger, "error">,
): Logger["error"] {
  const reportError = logger.error.bind(logger);

  return (message, options) => {
    const error = options?.error;
    const code = error && "code" in error ? error.code : undefined;
    const isWebSocketProxyError =
      message.includes("ws proxy error:") ||
      message.includes("ws proxy socket error:");

    if (
      isWebSocketProxyError &&
      typeof code === "string" &&
      EXPECTED_SOCKET_TEARDOWN_CODES.has(code)
    ) {
      return;
    }

    reportError(message, options);
  };
}
