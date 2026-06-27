import type { AcpAuthMethod } from "../../types/chat";
import { Button } from "../shared/Button";

interface AcpAuthControlsProps {
  authMethods: AcpAuthMethod[];
  disabled?: boolean;
  logoutSupported?: boolean;
  onAuthenticate?: (methodId: string) => void;
  onLogout?: () => void;
}

export function AcpAuthControls({
  authMethods,
  disabled = false,
  logoutSupported = false,
  onAuthenticate,
  onLogout,
}: AcpAuthControlsProps) {
  const canAuthenticate = authMethods.length > 0 && Boolean(onAuthenticate);
  const canLogout = logoutSupported && Boolean(onLogout);
  if (!canAuthenticate && !canLogout) return null;

  return (
    <div className="chat-input-model-controls" aria-label="ACP authentication">
      {canAuthenticate
        ? authMethods.map((method) => (
            <Button
              key={method.id}
              size="sm"
              variant="accent"
              disabled={disabled}
              title={method.description ?? method.name}
              onClick={() => onAuthenticate?.(method.id)}
            >
              Sign in: {method.name}
            </Button>
          ))
        : null}
      {canLogout ? (
        <Button
          size="sm"
          variant="destructive"
          disabled={disabled}
          title="Log out of this ACP provider"
          onClick={() => onLogout?.()}
        >
          Logout
        </Button>
      ) : null}
    </div>
  );
}
