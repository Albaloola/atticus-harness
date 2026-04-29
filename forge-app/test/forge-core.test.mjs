import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { builderPrompt } from "../src/main/tasks.mjs";
import { init, pathGate, reportFeatures, runOne, status } from "../src/main/forge-core.mjs";
import { runCommand } from "../src/main/git.mjs";

test("builder prompt keeps task bounded", () => {
  const prompt = builderPrompt({ id: "T-0001", title: "Test", allowedPaths: ["tests/"], forbiddenPaths: [".env"] });
  assert.match(prompt, /isolated git worktree/);
  assert.match(prompt, /Only modify allowed paths/);
});

test("report features include local reports-derived capabilities", () => {
  const names = reportFeatures().map((item) => item.name);
  assert.ok(names.includes("memory dreamer"));
  assert.ok(names.includes("usage-aware audit fields"));
  assert.ok(names.includes("council review in app path"));
});

test("status initializes local forge state in a git repo", async () => {
  const repo = await fs.mkdtemp(path.join(os.tmpdir(), "forge-app-test-"));
  await runCommand("git", ["init"], { cwd: repo });
  await init(repo);
  const result = await status(repo);
  assert.equal(result.repo, repo);
  assert.equal(result.state.running, false);
  assert.ok(Array.isArray(result.features));
});

test("runOne records app-path council and usage metadata", async () => {
  const repo = await seedRepo("forge-app-run-one-", "Exercise app-path council.");
  const engine = path.join(os.tmpdir(), `forge-app-engine-${process.pid}.mjs`);
  await fs.writeFile(engine, "import fs from 'node:fs/promises'; await fs.appendFile('FORGE_BACKLOG.md', '\\n- [x] Exercised by Forge app test.\\n');\n");

  const packet = await runOne(repo, { shellEngineCommand: `node ${engine}`, skipChecks: true, offlineReview: true });

  assert.equal(packet.finalDecision, "committed");
  assert.equal(packet.models.reviewer, "deepseek/deepseek-v4-flash");
  assert.equal(packet.review.mode, "offline_council");
  assert.ok(packet.reviewerVerdicts.some((verdict) => verdict.role === "security_reviewer"));
  assert.ok(packet.reviewerVerdicts.some((verdict) => verdict.role === "local_judge"));
  assert.deepEqual(Object.keys(packet.usage), ["prompt_tokens", "completion_tokens", "cached_tokens", "total_tokens", "total_cost_usd"]);
});

test("runOne rejects files outside task allowed paths", async () => {
  const repo = await seedRepo("forge-app-outside-", "Stay inside allowed paths.");
  const engine = path.join(os.tmpdir(), `forge-app-outside-${process.pid}.mjs`);
  await fs.writeFile(engine, `import fs from 'node:fs/promises';\nawait fs.writeFile('package.json', ${JSON.stringify('{"private":true}\n')});\n`);

  const packet = await runOne(repo, { shellEngineCommand: `node ${engine}`, skipChecks: true, offlineReview: true });

  assert.equal(packet.finalDecision, "rejected");
  assert.ok(packet.gateResults.some((gate) => gate.name === "path safety" && !gate.passed && gate.details.includes("outside allowed paths")));
});

test("pathGate treats allowed paths as repo-relative", () => {
  const policy = { forbiddenPaths: [] };

  assert.equal(pathGate(["src/tests/example.py"], policy, { allowedPaths: ["tests/"] }).passed, false);
  assert.equal(pathGate(["docs/README.md"], policy, { allowedPaths: ["README.md"] }).passed, false);
  assert.equal(pathGate(["tests/example.py"], policy, { allowedPaths: ["tests/"] }).passed, true);
});

test("runOne rescans after required checks mutate forbidden files", async () => {
  const repo = await seedRepo("forge-app-check-mutation-", "Catch post-check mutation.");
  const engine = path.join(os.tmpdir(), `forge-app-check-engine-${process.pid}.mjs`);
  const bin = await fs.mkdtemp(path.join(os.tmpdir(), "forge-app-bin-"));
  const fakePython = path.join(bin, "python");
  const oldPath = process.env.PATH;
  await fs.writeFile(engine, "import fs from 'node:fs/promises'; await fs.appendFile('FORGE_BACKLOG.md', '\\n- [x] Engine changed allowed file.\\n');\n");
  await fs.writeFile(fakePython, "#!/bin/sh\nprintf 'OPENROUTER_API_KEY=\\\"sk-this-is-not-real-token\\\"\\n' > .env\nexit 0\n");
  await fs.chmod(fakePython, 0o755);

  try {
    process.env.PATH = `${bin}:${oldPath ?? ""}`;
    const packet = await runOne(repo, { shellEngineCommand: `node ${engine}`, offlineReview: true });
    assert.equal(packet.finalDecision, "rejected");
    assert.ok(packet.gateResults.some((gate) => gate.name === "path safety" && !gate.passed && gate.details.includes(".env")));
    assert.ok(packet.gateResults.some((gate) => gate.name === "secret scan" && !gate.passed));
  } finally {
    process.env.PATH = oldPath;
  }
});

test("runOne scans staged changes before approval", async () => {
  const repo = await seedRepo("forge-app-staged-secret-", "Catch staged secret.");
  const engine = path.join(os.tmpdir(), `forge-app-staged-engine-${process.pid}.mjs`);
  await fs.writeFile(engine, "import fs from 'node:fs/promises';\nimport { execFileSync } from 'node:child_process';\nawait fs.writeFile('README.md', 'OPENROUTER_API_KEY=\\\"sk-this-is-not-real-token\\\"\\n');\nexecFileSync('git', ['add', 'README.md']);\nawait fs.appendFile('FORGE_BACKLOG.md', '\\n- [x] Staged secret path.\\n');\n");

  const packet = await runOne(repo, { shellEngineCommand: `node ${engine}`, skipChecks: true, offlineReview: true });

  assert.equal(packet.finalDecision, "rejected");
  assert.ok(packet.gateResults.some((gate) => gate.name === "secret scan" && !gate.passed));
});

test("runOne rejects reviewer repair verdict without blocking issues", async () => {
  const repo = await seedRepo("forge-app-reviewer-repair-", "Respect reviewer repair verdict.");
  const engine = path.join(os.tmpdir(), `forge-app-review-engine-${process.pid}.mjs`);
  const oldFetch = globalThis.fetch;
  const oldKey = process.env.OPENROUTER_API_KEY;
  await fs.writeFile(engine, "import fs from 'node:fs/promises'; await fs.appendFile('FORGE_BACKLOG.md', '\\n- [x] Reviewer repair path.\\n');\n");

  try {
    process.env.OPENROUTER_API_KEY = "test-key";
    globalThis.fetch = async () => new Response(JSON.stringify({
      choices: [{ message: { content: JSON.stringify({ role: "reviewer", verdict: "repair", confidence: 0.9, riskLevel: "medium", recommendedRepairs: ["tighten the diff"] }) } }],
      usage: { prompt_tokens: 3, completion_tokens: 2, total_tokens: 5 },
    }), { status: 200 });
    const packet = await runOne(repo, { shellEngineCommand: `node ${engine}`, skipChecks: true });
    assert.equal(packet.finalDecision, "rejected");
    assert.ok(packet.reviewerVerdicts.some((verdict) => verdict.role === "local_judge" && verdict.blockingIssues.some((issue) => issue.includes("Reviewer reviewer returned repair"))));
  } finally {
    globalThis.fetch = oldFetch;
    if (oldKey === undefined) delete process.env.OPENROUTER_API_KEY;
    else process.env.OPENROUTER_API_KEY = oldKey;
  }
});

async function seedRepo(prefix, title) {
  const repo = await fs.mkdtemp(path.join(os.tmpdir(), prefix));
  await runCommand("git", ["init"], { cwd: repo });
  await runCommand("git", ["config", "user.email", "forge@example.test"], { cwd: repo });
  await runCommand("git", ["config", "user.name", "Forge Test"], { cwd: repo });
  await fs.writeFile(path.join(repo, "FORGE_BACKLOG.md"), `# Forge Backlog\n\n- [ ] ${title}\n`);
  await runCommand("git", ["add", "FORGE_BACKLOG.md"], { cwd: repo });
  await runCommand("git", ["commit", "-m", "seed backlog"], { cwd: repo });
  return repo;
}
