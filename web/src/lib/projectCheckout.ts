/**
 * Shared copy and detection for "this machine has no checkout of the project"
 * (epic #19651). A project carries zero or one checkout per machine; every
 * surface that hits a checkout-bound endpoint reports the gap with one voice.
 */

export const NO_CHECKOUT_MESSAGE =
  "No checkout for this project on this machine";

/** Websocket `error` frame code the chat server sends when the session has no checkout. */
export const CHECKOUT_REQUIRED_CODE = "checkout_required";

/** Server exception name carried in a 409 `detail.error` for a missing checkout. */
const CHECKOUT_NOT_FOUND_ERROR = "CheckoutNotFoundError";

const NO_CHECKOUT_TEXT = /no checkout/i;

/**
 * True when a parsed HTTP error body reports a missing checkout. Accepts the
 * FastAPI shapes `{ detail: { error, message } }` and `{ detail: "..." }`.
 */
export function isNoCheckoutErrorBody(body: unknown): boolean {
  if (!body || typeof body !== "object") return false;
  const detail = "detail" in body ? body.detail : body;
  if (typeof detail === "string") return NO_CHECKOUT_TEXT.test(detail);
  if (!detail || typeof detail !== "object") return false;
  const { error, message } = detail as { error?: unknown; message?: unknown };
  return (
    error === CHECKOUT_NOT_FOUND_ERROR ||
    (typeof message === "string" && NO_CHECKOUT_TEXT.test(message))
  );
}

/** Same check over a raw response body (JSON when parseable, else plain text). */
export function isNoCheckoutErrorText(text: string): boolean {
  if (!text) return false;
  try {
    return isNoCheckoutErrorBody(JSON.parse(text));
  } catch {
    return NO_CHECKOUT_TEXT.test(text);
  }
}

/** Consume a failed Response and say whether it is the no-checkout case. */
export async function responseReportsNoCheckout(
  response: Response,
): Promise<boolean> {
  try {
    return isNoCheckoutErrorText(await response.text());
  } catch {
    return false;
  }
}
