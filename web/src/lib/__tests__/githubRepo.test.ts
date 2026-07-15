import { describe, expect, it } from "vitest";

import { isValidGithubRepoSlug } from "../githubRepo";

describe("isValidGithubRepoSlug", () => {
  it("allows double hyphens in repository names", () => {
    expect(isValidGithubRepoSlug("owner/repo--name")).toBe(true);
  });

  it("rejects double hyphens in owner names", () => {
    expect(isValidGithubRepoSlug("ow--ner/repo")).toBe(false);
  });
});
