export const manifest = {
  manifest_version: 3,
  name: "Gobby Annotate",
  version: "0.1.0",
  description:
    "Select page elements or regions, keep local notes, and export portable captures.",
  permissions: ["activeTab", "scripting", "storage", "downloads"],
  action: { default_title: "Annotate this page" },
  icons: { "128": "logo.png", "512": "logo.png" },
  background: { service_worker: "background.js", type: "module" },
};
