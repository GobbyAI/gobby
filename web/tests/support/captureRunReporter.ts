/**
 * Reporter entry point for the capture-run success attestation. Playwright
 * requires a reporter module's default export to be the reporter class, while
 * `captureRunFinalizer.ts` must default-export the globalTeardown function —
 * this shim bridges the two so the coordinator logic stays in one module.
 */
export { CaptureRunReporter as default } from "./captureRunFinalizer";
