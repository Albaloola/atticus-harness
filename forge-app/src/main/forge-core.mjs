#!/usr/bin/env node
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { builderPrompt, harvestTasks, selectTask } from "./tasks.mjs";
import { changedFiles, currentBranch, diff, diffStats, ensureClean, ensureGitRoot, forgeBranches, git, runCommand } from "./git.mjs";
import { ensureForgeDirs, ensureForgeProjectFiles, forgeDir, nowIso, readState, requestStop, resume, stopRequested, writeState } from "./state.mjs";
import { loadPolicy } from "./policy.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const appRoot = path.resolve(here, "../..");
export const MODEL_FLASH = "deepseek/deepseek-v4-flash";
export const MODEL_FLASH_NITRO = "deepseek/deepseek-v4-flash:nitro";
const OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1";
const OPENROUTER_PROVIDER = { allow_fallbacks: true, require_parameters: true, data_collection: "deny" };
const defaultEngine = process.env.FORGE_OPENCLAW_ENGINE || "/home/alba/open-systeme-Repo 1 Claude Code/openclaw.mjs";

export async function main(argv = process.argv.slice(2)) {
  const { command, options } = parseArgs(argv);
  const repo = path.resolve(options.repo || process.cwd());
  if (command === "features") return printJson(reportFeatures());
  if (command === "init") return printJson(await init(repo));
  if (command === "status") return printJson(await status(repo));
  if (command === "stop") return printJson(await stop(repo));
  if (command === "resume") return printJson(await doResume(repo));
  if (command === "cleanup") return printJson(await cleanup(repo));
  if (command === "dream") return printJson(await dream(repo));
  if (command === "run-one") return printJson(await runOne(repo, options));
  if (command === "loop") return printJson(await loop(repo, options));
  throw new Error(`unknown command ${command}`);
}

export async function init(repo) {
  await ensureForgeDirs(repo);
  await ensureForgeProjectFiles(repo);
  return { initialized: true, repo, state: await readState(repo), features: reportFeatures() };
}

export async function status(repo) {
  await ensureForgeDirs(repo);
  return {
    repo,
    state: await readState(repo),
    branches: await forgeBranches(repo),
    latestAudit: await latestAuditSummary(repo),
    features: reportFeatures(),
  };
}

export async function runOne(repo, options = {}) {
  await ensureForgeDirs(repo);
  await ensureGitRoot(repo);
  await ensureClean(repo);
  const policy = loadPolicy(options.policy);
  const task = selectTask(await harvestTasks(repo, policy));
  const start = nowIso();
  await writeState(repo, { running: true, currentTask: task, lastIteration: task.id });
  let worktree;
  const packet = {
    iterationId: task.id,
    timestampStart: start,
    timestampEnd: "",
    targetRepo: repo,
    baseBranch: await currentBranch(repo),
    branchName: "",
    worktreePath: "",
    task,
    engine: { name: "openclaw_claude_code_style", command: options.engineCommand || defaultEngine, model: MODEL_FLASH_NITRO, provider: "openrouter" },
    models: { builder: MODEL_FLASH_NITRO, reviewer: MODEL_FLASH, provider: "openrouter", providerOptions: OPENROUTER_PROVIDER },
    engineResult: null,
    changedFiles: [],
    diffStats: { filesChanged: 0, linesAdded: 0, linesDeleted: 0 },
    gateResults: [],
    reviewerVerdicts: [],
    usage: emptyUsage(),
    cost: emptyUsage(),
    review: { mode: options.offlineReview ? "offline_council" : "openrouter_or_offline", model: MODEL_FLASH, provider: "openrouter" },
    reportFeatures: reportFeatures(),
    finalDecision: "failed",
    commitSha: "",
    riskScore: 0,
  };
  try {
    worktree = await createWorktree(repo, task.title);
    packet.branchName = worktree.branch;
    packet.worktreePath = worktree.path;
    await writeState(repo, { lastBranch: worktree.branch });
    const engineResult = await runEngine(worktree.path, task, options);
    packet.engineResult = engineResult;
    await fs.rm(path.join(worktree.path, ".forge_task.md"), { force: true });
    const evaluation = await evaluate(worktree.path, policy, engineResult, options, task);
    Object.assign(packet, evaluation.packetFields);
    packet.reviewerVerdicts = evaluation.verdicts;
    packet.gateResults = evaluation.gates;
    if (evaluation.approved) {
      packet.commitSha = await commit(worktree.path, task.title);
      packet.finalDecision = "committed";
    } else {
      packet.finalDecision = "rejected";
    }
    packet.timestampEnd = nowIso();
    return packet;
  } finally {
    if (worktree) {
      await git(repo, ["worktree", "remove", "--force", worktree.path], { check: false, timeoutMs: 180_000 });
      if (packet.finalDecision !== "committed") await git(repo, ["branch", "-D", worktree.branch], { check: false });
      await git(repo, ["worktree", "prune"], { check: false });
    }
    if (!packet.timestampEnd) packet.timestampEnd = nowIso();
    await writeAudit(repo, packet);
    await writeState(repo, {
      running: false,
      currentTask: null,
      lastIteration: packet.iterationId,
      lastBranch: packet.branchName,
      lastCommitSha: packet.commitSha,
      consecutiveFailures: packet.finalDecision === "committed" ? 0 : 1,
    });
  }
}

export async function loop(repo, options = {}) {
  const maxIterations = Number(options.maxIterations || 50);
  const delayMs = Number(options.delaySeconds || 120) * 1000;
  let iterations = 0;
  let failures = 0;
  await writeState(repo, { running: true });
  while (!(await stopRequested(repo)) && iterations < maxIterations && failures < 3) {
    try {
      const packet = await runOne(repo, options);
      iterations += 1;
      failures = packet.finalDecision === "committed" ? 0 : failures + 1;
    } catch {
      failures += 1;
    }
    if (delayMs > 0 && !(await stopRequested(repo)) && iterations < maxIterations) await sleep(delayMs);
  }
  await writeState(repo, { running: false, consecutiveFailures: failures });
  return { iterations, consecutiveFailures: failures, stopped: await stopRequested(repo) };
}

export async function stop(repo) {
  await requestStop(repo);
  return { stopped: true };
}

export async function doResume(repo) {
  await resume(repo);
  return { resumed: true };
}

export async function cleanup(repo) {
  const result = await git(repo, ["worktree", "prune"], { check: false });
  return { exitCode: result.exitCode, stdout: result.stdout, stderr: result.stderr };
}

export async function dream(repo) {
  await ensureForgeDirs(repo);
  const latest = await latestAuditSummary(repo);
  const observation = latest ? `Latest audit ${latest.iterationId}: ${latest.finalDecision}` : "No audits yet.";
  const memoryPath = path.join(forgeDir(repo), "memory", "decisions.md");
  await fs.appendFile(memoryPath, `\n- ${nowIso()}: ${observation}\n`);
  return { newObservations: [observation], reportDerivedFeatures: reportFeatures().map((item) => item.name) };
}

export function reportFeatures() {
  return [
    { name: "stable/dynamic prompt layout", source: "OPTIMIZATION-TARGETS.md", implemented: "builder prompts keep policy/schema before task diff data" },
    { name: "usage-aware audit fields", source: "CHANGES-008-token-usage.md", implemented: "audit packets reserve token/cache/cost fields and expose them in GUI state" },
    { name: "council review in app path", source: "Forge mission", implemented: "Node app core runs reviewer/security/minimalist/domain/judge verdicts with OpenRouter DeepSeek defaults and offline fallback" },
    { name: "memory dreamer", source: "MEMORY-AGENT-TARGETS.md", implemented: "dream command writes post-iteration observations to .forge/memory" },
    { name: "semantic backlog prioritization", source: "CHANGES-009-semantic-reranking.md", implemented: "task selection scores backlog/TODO candidates before execution" },
    { name: "safe local process control", source: "OpenClaw src/node-host/invoke.ts", implemented: "Node core uses spawn with cwd, timeout, captured stdout/stderr, and git prompt blocking" },
  ];
}

async function createWorktree(repo, title) {
  const date = new Date().toISOString().slice(0, 10).replaceAll("-", "");
  const slug = title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 48) || "task";
  const existing = await forgeBranches(repo);
  const sequence = String(existing.filter((branch) => branch.includes(`forge/${date}-`)).length + 1).padStart(3, "0");
  const component = `${date}-${sequence}-${slug}`;
  const branch = `forge/${component}`;
  const worktree = path.join(forgeDir(repo), "worktrees", component);
  await fs.mkdir(path.dirname(worktree), { recursive: true });
  await git(repo, ["check-ref-format", "--branch", branch]);
  await git(repo, ["worktree", "add", "-b", branch, worktree, "HEAD"], { timeoutMs: 180_000 });
  return { branch, path: worktree };
}

async function runEngine(worktree, task, options) {
  const prompt = builderPrompt(task);
  const promptPath = path.join(worktree, ".forge_task.md");
  await fs.writeFile(promptPath, prompt);
  const commandLine = options.shellEngineCommand || options.engineCommand || `node ${JSON.stringify(defaultEngine)}`;
  const [command, ...args] = splitCommand(commandLine);
  const finalArgs = options.shellEngineCommand ? args : [...args, "agent", "--message", prompt, "--timeout", String(options.engineTimeoutSeconds || 2700)];
  const started = Date.now();
  const result = await runCommand(command, finalArgs, { cwd: worktree, timeoutMs: Number(options.engineTimeoutSeconds || 2700) * 1000 });
  return { ...result, durationSeconds: (Date.now() - started) / 1000 };
}

async function evaluate(worktree, policy, engineResult, options, task) {
  const gates = [{ name: "engine exit", passed: engineResult.exitCode === 0, details: `exit ${engineResult.exitCode}` }];
  for (const check of options.skipChecks ? [] : policy.requiredChecks) {
    const result = await runCommand("sh", ["-c", check], { cwd: worktree, timeoutMs: 900_000 });
    gates.push({ name: `command: ${check}`, passed: result.exitCode === 0, details: `exit ${result.exitCode}`, stdout: result.stdout, stderr: result.stderr });
  }
  const files = await changedFiles(worktree);
  const patch = await diff(worktree);
  const stats = await diffStats(worktree);
  gates.push(pathGate(files, policy, task), diffLimitGate(stats, policy), secretGate(patch));
  const council = await reviewCouncil({ task, files, patch, stats, gates, policy, engineResult, options });
  const approved = council.finalVerdict.verdict === "approve";
  return {
    approved,
    gates,
    verdicts: council.verdicts,
    packetFields: { changedFiles: files, diffStats: stats, riskScore: council.riskScore, usage: council.usage, cost: council.cost, review: council.review },
  };
}

async function commit(worktree, title) {
  await git(worktree, ["add", "-A"]);
  const clean = await git(worktree, ["diff", "--cached", "--quiet"], { check: false });
  if (clean.exitCode === 0) throw new Error("no staged changes to commit");
  await git(worktree, ["commit", "-m", `chore: ${title.toLowerCase().slice(0, 60)}`, "-m", "Generated by Atticus Forge App after local gates."]);
  const result = await git(worktree, ["rev-parse", "HEAD"]);
  return result.stdout.trim();
}

export function pathGate(files, policy, task = {}) {
  const violations = [];
  const allowedPaths = Array.isArray(task.allowedPaths) ? task.allowedPaths : [];
  for (const file of files) {
    if (allowedPaths.length > 0 && !allowedPaths.some((allowed) => matchesAllowedPath(file, allowed))) {
      violations.push(`${file} is outside allowed paths: ${allowedPaths.join(", ")}`);
    }
    for (const forbidden of policy.forbiddenPaths) {
      if (matchesForbiddenPath(file, forbidden)) violations.push(`${file} matches ${forbidden}`);
    }
  }
  return { name: "path safety", passed: violations.length === 0, details: violations.join("\n") || "ok" };
}

function diffLimitGate(stats, policy) {
  const total = stats.linesAdded + stats.linesDeleted;
  const failures = [];
  if (stats.filesChanged > policy.diffLimits.maxFilesChanged) failures.push("too many files");
  if (total > policy.diffLimits.maxDiffLines) failures.push("too many diff lines");
  if (stats.linesDeleted > policy.diffLimits.maxDeletedLines) failures.push("too many deletions");
  return { name: "diff limits", passed: failures.length === 0, details: failures.join("; ") || "ok" };
}

function secretGate(patch) {
  const found = /api[_-]?key\s*[:=]|bearer\s+[a-z0-9_.-]{20,}|BEGIN .*PRIVATE KEY|password\s*[:=]/i.test(patch);
  return { name: "secret scan", passed: !found, details: found ? "possible secret in diff" : "ok" };
}

async function reviewCouncil({ task, files, patch, stats, gates, policy, engineResult, options }) {
  const general = await reviewerVerdict({ task, files, patch, stats, gates, engineResult, options });
  const verdicts = [general, securityVerdict(files, gates), minimalistVerdict(files, stats, policy)];
  if (policy.name === "atticus" || files.some((file) => file.startsWith("atticus/"))) verdicts.push(domainVerdict(files));
  const finalVerdict = judgeVerdict(gates, patch, verdicts);
  verdicts.push(finalVerdict);
  const usage = aggregateUsage(verdicts);
  return {
    verdicts,
    finalVerdict,
    usage,
    cost: usage,
    riskScore: riskScore(verdicts, stats),
    review: { mode: general.mode, model: MODEL_FLASH, provider: "openrouter", offline: general.offline === true },
  };
}

async function reviewerVerdict({ task, files, patch, stats, gates, engineResult, options }) {
  const missingKey = !process.env.OPENROUTER_API_KEY;
  if (options.offlineReview || missingKey) {
    const note = missingKey && !options.offlineReview ? ["OPENROUTER_API_KEY is not set; used local offline reviewer."] : [];
    return localReviewerVerdict(gates, patch, note);
  }
  try {
    const response = await openRouterReview({ task, files, patch, stats, gates, engineResult });
    return coerceModelVerdict(response.content, response.usage);
  } catch (error) {
    return makeVerdict({
      role: "reviewer",
      verdict: "repair",
      confidence: 0.45,
      riskLevel: "medium",
      blockingIssues: [`OpenRouter reviewer failed: ${error.message}`],
      recommendedRepairs: ["Run with --offline-review or restore OpenRouter reviewer access."],
      mode: "openrouter_error",
      offline: false,
    });
  }
}

function localReviewerVerdict(gates, patch, nonBlockingIssues = []) {
  const blockers = gates.filter((gate) => !gate.passed).map((gate) => gate.name);
  if (!patch.trim()) blockers.push("No diff was produced.");
  return makeVerdict({
    role: "reviewer",
    verdict: blockers.length > 0 ? "reject" : "approve",
    confidence: 0.76,
    riskLevel: blockers.length > 0 ? "high" : "low",
    blockingIssues: blockers,
    nonBlockingIssues,
    recommendedRepairs: blockers,
    mode: "offline_council",
    offline: true,
  });
}

function securityVerdict(files, gates) {
  const securityFailures = gates.filter((gate) => !gate.passed && ["secret scan", "path safety"].includes(gate.name)).map((gate) => `${gate.name}: ${gate.details}`);
  return makeVerdict({
    role: "security_reviewer",
    verdict: securityFailures.length > 0 ? "reject" : "approve",
    confidence: 0.82,
    riskLevel: securityFailures.length > 0 ? "high" : "low",
    blockingIssues: securityFailures,
    recommendedRepairs: securityFailures,
    filesOfConcern: securityFailures.length > 0 ? files : [],
    mode: "offline_council",
    offline: true,
  });
}

function minimalistVerdict(files, stats, policy) {
  const total = stats.linesAdded + stats.linesDeleted;
  const blockers = [];
  if (stats.filesChanged > policy.diffLimits.maxFilesChanged) blockers.push("Diff changes too many files.");
  if (total > policy.diffLimits.maxDiffLines) blockers.push("Diff is larger than policy limit.");
  return makeVerdict({
    role: "minimalist_reviewer",
    verdict: blockers.length > 0 ? "reject" : "approve",
    confidence: 0.78,
    riskLevel: blockers.length > 0 ? "medium" : "low",
    blockingIssues: blockers,
    recommendedRepairs: blockers,
    filesOfConcern: blockers.length > 0 ? files : [],
    mode: "offline_council",
    offline: true,
  });
}

function domainVerdict(files) {
  return makeVerdict({
    role: "domain_reviewer",
    verdict: "approve",
    confidence: 0.68,
    riskLevel: files.some((file) => file.startsWith("atticus/")) ? "medium" : "low",
    nonBlockingIssues: ["Local Atticus domain review only; keep legal-critical changes small and audited."],
    filesOfConcern: files.filter((file) => file.startsWith("atticus/")),
    mode: "offline_council",
    offline: true,
  });
}

function judgeVerdict(gates, patch, verdicts) {
  const blockers = [
    ...gates.filter((gate) => !gate.passed).map((gate) => `Gate failed: ${gate.name}`),
    ...verdicts.filter((verdict) => verdict.verdict !== "approve").map((verdict) => `Reviewer ${verdict.role} returned ${verdict.verdict}`),
    ...verdicts.flatMap((verdict) => verdict.blockingIssues ?? []),
    ...verdicts.flatMap((verdict) => (verdict.recommendedRepairs ?? []).map((repair) => `${verdict.role} repair: ${repair}`)),
  ];
  if (!patch.trim()) blockers.push("No diff was produced.");
  return makeVerdict({
    role: "local_judge",
    verdict: blockers.length > 0 ? "reject" : "approve",
    confidence: 0.86,
    riskLevel: blockers.length > 0 ? "high" : "low",
    blockingIssues: Array.from(new Set(blockers)),
    recommendedRepairs: Array.from(new Set(blockers)),
    mode: "offline_council",
    offline: true,
  });
}

async function openRouterReview({ task, files, patch, stats, gates, engineResult }) {
  const response = await fetch(`${OPENROUTER_BASE_URL}/chat/completions`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${process.env.OPENROUTER_API_KEY}`,
      "Content-Type": "application/json",
      "HTTP-Referer": "https://local.atticus-forge",
      "X-OpenRouter-Title": "Atticus Forge App",
    },
    body: JSON.stringify({
      model: MODEL_FLASH,
      temperature: 0.1,
      max_tokens: 4096,
      response_format: { type: "json_object" },
      provider: OPENROUTER_PROVIDER,
      messages: [
        { role: "system", content: "You are the Forge reviewer. Return strict JSON with role, verdict, confidence, riskLevel, blockingIssues, nonBlockingIssues, recommendedRepairs, and filesOfConcern." },
        { role: "user", content: JSON.stringify({ task, changedFiles: files, diffStats: stats, diff: patch.slice(-60000), gateResults: gates, engineResult: { exitCode: engineResult.exitCode, stderr: engineResult.stderr.slice(-4000) } }) },
      ],
    }),
  });
  const text = await response.text();
  if (!response.ok) throw new Error(`OpenRouter HTTP ${response.status}: ${text.slice(0, 240)}`);
  const raw = JSON.parse(text);
  const content = raw?.choices?.[0]?.message?.content;
  const parsed = typeof content === "string" ? JSON.parse(content) : content;
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("OpenRouter reviewer returned non-object JSON");
  return { content: parsed, usage: normalizeUsage(raw.usage ?? {}) };
}

function coerceModelVerdict(raw, usage) {
  const allowed = new Set(["approve", "repair", "reject"]);
  const verdict = allowed.has(String(raw.verdict)) ? String(raw.verdict) : "reject";
  return makeVerdict({
    role: String(raw.role || "reviewer"),
    verdict,
    confidence: Number(raw.confidence ?? 0),
    riskLevel: String(raw.riskLevel || raw.risk_level || (verdict === "approve" ? "low" : "high")),
    blockingIssues: stringList(raw.blockingIssues ?? raw.blocking_issues),
    nonBlockingIssues: stringList(raw.nonBlockingIssues ?? raw.non_blocking_issues),
    recommendedRepairs: stringList(raw.recommendedRepairs ?? raw.recommended_repairs),
    filesOfConcern: stringList(raw.filesOfConcern ?? raw.files_of_concern),
    usage,
    cost: usage,
    mode: "openrouter",
    offline: false,
  });
}

function makeVerdict({ role, verdict, confidence, riskLevel, blockingIssues = [], nonBlockingIssues = [], recommendedRepairs = [], filesOfConcern = [], usage = emptyUsage(), cost = emptyUsage(), mode = "offline_council", offline = true }) {
  return { role, verdict, confidence, riskLevel, blockingIssues, nonBlockingIssues, recommendedRepairs, filesOfConcern, model: MODEL_FLASH, provider: "openrouter", usage, cost, mode, offline };
}

function aggregateUsage(verdicts) {
  const usage = emptyUsage();
  for (const verdict of verdicts) {
    const item = normalizeUsage(verdict.usage ?? {});
    usage.prompt_tokens += item.prompt_tokens;
    usage.completion_tokens += item.completion_tokens;
    usage.cached_tokens += item.cached_tokens;
    usage.total_tokens += item.total_tokens;
    usage.total_cost_usd += item.total_cost_usd;
  }
  return usage;
}

function normalizeUsage(raw) {
  const details = raw.prompt_tokens_details && typeof raw.prompt_tokens_details === "object" ? raw.prompt_tokens_details : {};
  return {
    prompt_tokens: numberValue(raw.prompt_tokens),
    completion_tokens: numberValue(raw.completion_tokens),
    cached_tokens: numberValue(raw.cached_tokens ?? raw.cache_read_input_tokens ?? details.cached_tokens ?? details.cache_read_tokens),
    total_tokens: numberValue(raw.total_tokens),
    total_cost_usd: numberValue(raw.total_cost_usd ?? raw.total_cost ?? raw.cost),
  };
}

function emptyUsage() {
  return { prompt_tokens: 0, completion_tokens: 0, cached_tokens: 0, total_tokens: 0, total_cost_usd: 0 };
}

function stringList(value) {
  return Array.isArray(value) ? value.map((item) => String(item)) : [];
}

function numberValue(value) {
  const parsed = Number(value ?? 0);
  return Number.isFinite(parsed) ? parsed : 0;
}

function riskScore(verdicts, stats) {
  const reviewerRisk = verdicts.reduce((total, verdict) => total + (verdict.riskLevel === "high" ? 4 : verdict.riskLevel === "medium" ? 1.5 : 0) + (verdict.blockingIssues?.length ?? 0) * 2, 0);
  return Math.round((reviewerRisk + stats.filesChanged * 0.2) * 100) / 100;
}

async function writeAudit(repo, packet) {
  const dir = path.join(forgeDir(repo), "audit", packet.timestampStart.slice(0, 10).replaceAll("-", ""), packet.iterationId);
  await fs.mkdir(dir, { recursive: true });
  await fs.writeFile(path.join(dir, "report.json"), `${JSON.stringify(packet, null, 2)}\n`);
}

async function latestAuditSummary(repo) {
  const root = path.join(forgeDir(repo), "audit");
  let reports = [];
  try {
    for (const day of await fs.readdir(root)) {
      for (const id of await fs.readdir(path.join(root, day))) reports.push(path.join(root, day, id, "report.json"));
    }
  } catch {
    return null;
  }
  reports = (await Promise.all(reports.map(async (file) => ({ file, stat: await fs.stat(file).catch(() => null) })))).filter((item) => item.stat).sort((a, b) => b.stat.mtimeMs - a.stat.mtimeMs);
  if (!reports[0]) return null;
  const data = JSON.parse(await fs.readFile(reports[0].file, "utf8"));
  return { path: reports[0].file, iterationId: data.iterationId, finalDecision: data.finalDecision, branchName: data.branchName, commitSha: data.commitSha, riskScore: data.riskScore };
}

function parseArgs(argv) {
  const command = argv[0] && !argv[0].startsWith("--") ? argv[0] : "status";
  const rest = command === argv[0] ? argv.slice(1) : argv;
  const options = {};
  for (let i = 0; i < rest.length; i += 1) {
    const item = rest[i];
    if (!item.startsWith("--")) continue;
    const key = item.slice(2).replace(/-([a-z])/g, (_, char) => char.toUpperCase());
    const next = rest[i + 1];
    if (!next || next.startsWith("--")) options[key] = true;
    else {
      options[key] = next;
      i += 1;
    }
  }
  return { command, options };
}

function splitCommand(commandLine) {
  return commandLine.match(/(?:[^\s"']+|"[^"]*"|'[^']*')+/g)?.map((part) => part.replace(/^['"]|['"]$/g, "")) ?? [];
}

function globRegex(glob) {
  return new RegExp(`^${glob.replace(/[.+^${}()|[\]\\]/g, "\\$&").replaceAll("*", ".*")}$`);
}

function matchesAllowedPath(file, pattern) {
  const normalized = file.replaceAll("\\", "/");
  const rule = String(pattern).replaceAll("\\", "/");
  if (!rule || rule === "." || rule === "./" || rule === "**") return true;
  if (rule.endsWith("/")) return normalized.startsWith(rule);
  return normalized === rule || globRegex(rule).test(normalized);
}

function matchesForbiddenPath(file, pattern) {
  const normalized = file.replaceAll("\\", "/");
  const rule = String(pattern).replaceAll("\\", "/");
  if (!rule || rule === "." || rule === "./" || rule === "**") return true;
  if (rule.endsWith("/")) return normalized.startsWith(rule) || normalized.includes(`/${rule}`);
  const base = path.basename(normalized);
  const regex = globRegex(rule);
  return normalized === rule || base === rule || regex.test(normalized) || regex.test(base);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function printJson(value) {
  process.stdout.write(`${JSON.stringify(value, null, 2)}\n`);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((error) => {
    process.stderr.write(`${JSON.stringify({ error: error.message }, null, 2)}\n`);
    process.exitCode = 2;
  });
}
