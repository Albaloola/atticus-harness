import { spawn } from "node:child_process";
import path from "node:path";

export function runCommand(command, args, options = {}) {
  return new Promise((resolve) => {
    const child = spawn(command, args, {
      cwd: options.cwd,
      shell: false,
      env: {
        ...process.env,
        GIT_MASTER: "1",
        GIT_TERMINAL_PROMPT: "0",
        GIT_ASKPASS: "",
        GCM_INTERACTIVE: "never",
        GIT_EDITOR: ":",
        GIT_PAGER: "cat",
        ...(options.env ?? {}),
      },
    });
    let stdout = "";
    let stderr = "";
    const timer = options.timeoutMs
      ? setTimeout(() => {
          child.kill("SIGTERM");
        }, options.timeoutMs)
      : undefined;
    child.stdout?.on("data", (chunk) => {
      stdout += String(chunk);
    });
    child.stderr?.on("data", (chunk) => {
      stderr += String(chunk);
    });
    child.on("close", (code, signal) => {
      if (timer) clearTimeout(timer);
      resolve({ command: [command, ...args].join(" "), exitCode: code ?? 1, signal, stdout, stderr });
    });
    child.on("error", (error) => {
      if (timer) clearTimeout(timer);
      resolve({ command: [command, ...args].join(" "), exitCode: 1, signal: null, stdout, stderr: String(error) });
    });
    if (options.input) {
      child.stdin?.write(options.input);
      child.stdin?.end();
    }
  });
}

export async function git(repo, args, options = {}) {
  const result = await runCommand("git", args, { cwd: repo, timeoutMs: options.timeoutMs ?? 120_000 });
  if (options.check !== false && result.exitCode !== 0) {
    throw new Error(`git ${args.join(" ")} failed: ${result.stderr || result.stdout}`);
  }
  return result;
}

export async function ensureGitRoot(repo) {
  const result = await git(repo, ["rev-parse", "--show-toplevel"]);
  const root = path.resolve(result.stdout.trim());
  if (root !== path.resolve(repo)) {
    throw new Error(`--repo must point to git root ${root}`);
  }
}

export async function ensureClean(repo) {
  const result = await git(repo, ["status", "--porcelain"]);
  const dirty = result.stdout
    .split("\n")
    .filter(Boolean)
    .filter((line) => !line.includes(" .forge/"));
  if (dirty.length > 0) {
    throw new Error(`target repo is dirty; refusing autonomous run:\n${dirty.slice(0, 30).join("\n")}`);
  }
}

export async function currentBranch(repo) {
  const result = await git(repo, ["branch", "--show-current"]);
  return result.stdout.trim() || "HEAD";
}

export async function forgeBranches(repo) {
  const result = await git(repo, ["branch", "--list", "forge/*"], { check: false });
  return result.stdout
    .split("\n")
    .map((line) => line.trim().replace(/^\*\s*/, ""))
    .filter(Boolean);
}

export async function changedFiles(repo) {
  const result = await git(repo, ["status", "--porcelain"]);
  return Array.from(
    new Set(
      result.stdout
        .split("\n")
        .filter(Boolean)
        .map((line) => {
          const raw = line.slice(3).trim();
          return raw.includes(" -> ") ? raw.split(" -> ").at(-1) : raw;
        }),
    ),
  ).sort();
}

export async function diff(repo) {
  await git(repo, ["add", "-N", "."], { check: false });
  const staged = await git(repo, ["diff", "--cached", "--binary"]);
  const unstaged = await git(repo, ["diff", "--binary"]);
  return [staged.stdout, unstaged.stdout].filter(Boolean).join("\n");
}

export async function diffStats(repo) {
  await git(repo, ["add", "-N", "."], { check: false });
  const staged = await git(repo, ["diff", "--cached", "--numstat"]);
  const unstaged = await git(repo, ["diff", "--numstat"]);
  const byFile = new Map();
  for (const line of `${staged.stdout}\n${unstaged.stdout}`.split("\n")) {
    if (!line.trim()) continue;
    const [added, deleted, file] = line.split("\t");
    const current = byFile.get(file) ?? { added: 0, deleted: 0 };
    if (/^\d+$/.test(added ?? "")) current.added += Number(added);
    if (/^\d+$/.test(deleted ?? "")) current.deleted += Number(deleted);
    byFile.set(file, current);
  }
  let filesChanged = 0;
  let linesAdded = 0;
  let linesDeleted = 0;
  for (const { added, deleted } of byFile.values()) {
    filesChanged += 1;
    linesAdded += added;
    linesDeleted += deleted;
  }
  return { filesChanged, linesAdded, linesDeleted };
}
