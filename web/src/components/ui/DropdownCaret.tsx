/* Canonical caret for translucent dropdown triggers (command bar session
 * switcher, activity panel tab selector, …). One size and color treatment
 * everywhere — see .impeccable.md "Canonical Components". */
export function DropdownCaret({ open = false }: { open?: boolean }) {
  return (
    <span className="dropdown-caret" aria-hidden="true">
      <svg
        viewBox="0 0 16 16"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        {open ? (
          <polyline points="4 10 8 6 12 10" />
        ) : (
          <polyline points="4 6 8 10 12 6" />
        )}
      </svg>
    </span>
  )
}
