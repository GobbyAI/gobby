import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@fontsource-variable/geist";
import "@fontsource-variable/jetbrains-mono";
import "./styles/index.css";
import App from "./App";
import { AppErrorBoundary } from "./components/app/AppErrorBoundary";
import { HitAreaHarness } from "./components/dev/HitAreaHarness";
import { isHitAreaHarnessRequested } from "./components/dev/hitAreaHarnessConfig";
import { TierPreview } from "./components/dev/TierPreview";
import {
  applyPointerOverride,
  isTierPreviewRequested,
} from "./components/dev/tierPreviewConfig";

applyPointerOverride(window.location.search, document.documentElement);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {isTierPreviewRequested(window.location.search) ? (
      <TierPreview />
    ) : isHitAreaHarnessRequested(window.location.search) ? (
      <HitAreaHarness />
    ) : (
      <AppErrorBoundary
        activeTab="application"
        onReturnToChat={() => {
          window.location.reload();
        }}
      >
        <App />
      </AppErrorBoundary>
    )}
  </StrictMode>,
);
