const GITHUB_OWNER_RE = /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$/
const GITHUB_REPO_RE = /^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$/

export function isValidGithubRepoSlug(value: string | null | undefined): value is string {
  if (typeof value !== 'string') return false
  const parts = value.split('/')
  if (parts.length !== 2) return false
  const [owner, repo] = parts
  return (
    GITHUB_OWNER_RE.test(owner) &&
    !owner.includes('--') &&
    GITHUB_REPO_RE.test(repo) &&
    !repo.includes('..') &&
    !repo.includes('--')
  )
}
