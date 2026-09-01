import { NO_CHECKOUT_MESSAGE } from "../../lib/projectCheckout";

/**
 * Composer notice for a project this machine has no checkout of: chat cannot
 * run against it here. Warning surface plus an icon so the state reads in
 * grayscale (.impeccable.md: color is the fourth signal, never the first).
 */
export function CheckoutRequiredBanner() {
  return (
    <div
      role="status"
      data-testid="checkout-required-banner"
      className="mx-3 mb-1 flex shrink-0 items-center gap-2 rounded-md bg-warning/20 px-2.5 py-1.5 text-xs text-warning-foreground"
    >
      <WarningIcon />
      <span>{NO_CHECKOUT_MESSAGE}</span>
    </div>
  );
}

function WarningIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className="shrink-0"
      aria-hidden="true"
    >
      <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" />
      <line x1="12" y1="9" x2="12" y2="13" />
      <line x1="12" y1="17" x2="12.01" y2="17" />
    </svg>
  );
}
