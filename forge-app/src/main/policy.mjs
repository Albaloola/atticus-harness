export const DEFAULT_POLICY = {
  name: "default",
  forbiddenPaths: [".env", ".env.*", "secrets/", "private/", "evidence/", "court_bundles/", "node_modules/", ".git/"],
  requiredChecks: ["python -m pytest"],
  diffLimits: { maxFilesChanged: 8, maxDiffLines: 800, maxDeletedLines: 500 },
};

export const ATTICUS_POLICY = {
  ...DEFAULT_POLICY,
  name: "atticus",
  forbiddenPaths: [...DEFAULT_POLICY.forbiddenPaths, "case_materials/originals/"],
  requiredPrinciples: [
    "Do not weaken citation validation.",
    "Do not mutate original evidence.",
    "Do not allow candidate model output into trusted memory without validation.",
    "Do not weaken reducer-only canonical writes.",
    "Do not remove audit logs.",
  ],
  requiredChecks: ["python -m pytest", "ruff check ."],
};

export function loadPolicy(name = "default") {
  return name === "atticus" ? ATTICUS_POLICY : DEFAULT_POLICY;
}
