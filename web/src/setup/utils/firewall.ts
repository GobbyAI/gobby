import { existsSync } from "fs";
import { dirname, join } from "path";
import { fileURLToPath } from "url";

export function resolveFirewallScriptPath(
  moduleUrl = import.meta.url,
  installDir = process.env.GOBBY_INSTALL_DIR,
): string | null {
  const moduleDir = dirname(fileURLToPath(moduleUrl));
  const candidates = [
    ...(installDir
      ? [join(installDir, "shared", "scripts", "setup-firewall.sh")]
      : []),
    join(moduleDir, "setup-firewall.sh"),
    join(moduleDir, "..", "scripts", "setup-firewall.sh"),
  ];

  return candidates.find((candidate) => existsSync(candidate)) ?? null;
}
