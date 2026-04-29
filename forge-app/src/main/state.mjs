import fs from "node:fs/promises";
import path from "node:path";

export function forgeDir(repo) {
  return path.join(repo, ".forge");
}

export function nowIso() {
  return new Date().toISOString();
}

export async function ensureForgeDirs(repo) {
  const base = forgeDir(repo);
  await fs.mkdir(path.join(base, "audit"), { recursive: true });
  await fs.mkdir(path.join(base, "memory"), { recursive: true });
  await fs.mkdir(path.join(base, "worktrees"), { recursive: true });
  await writeIfMissing(path.join(base, "memory", "backlog.json"), "[]\n");
  await writeIfMissing(path.join(base, "state.json"), `${JSON.stringify(defaultState(), null, 2)}\n`);
}

export async function ensureForgeProjectFiles(repo) {
  await writeIfMissing(path.join(repo, "FORGE_MISSION.md"), "# Forge Mission\n\nLocal autonomous branch factory.\n");
  await writeIfMissing(path.join(repo, "FORGE_BACKLOG.md"), "# Forge Backlog\n\n- [ ] Add the first small safe improvement.\n");
}

export function defaultState() {
  return {
    running: false,
    currentTask: null,
    lastIteration: "",
    lastBranch: "",
    lastCommitSha: "",
    consecutiveFailures: 0,
    costToday: 0,
    updatedAt: nowIso(),
  };
}

export async function readState(repo) {
  await ensureForgeDirs(repo);
  try {
    const parsed = JSON.parse(await fs.readFile(path.join(forgeDir(repo), "state.json"), "utf8"));
    return parsed && typeof parsed === "object" ? parsed : defaultState();
  } catch {
    return defaultState();
  }
}

export async function writeState(repo, updates) {
  const next = { ...(await readState(repo)), ...updates, updatedAt: nowIso() };
  await fs.writeFile(path.join(forgeDir(repo), "state.json"), `${JSON.stringify(next, null, 2)}\n`);
  return next;
}

export async function requestStop(repo) {
  await ensureForgeDirs(repo);
  await fs.writeFile(path.join(forgeDir(repo), "STOP"), `stop requested at ${nowIso()}\n`);
  await writeState(repo, { running: false });
}

export async function resume(repo) {
  await fs.rm(path.join(forgeDir(repo), "STOP"), { force: true });
  await writeState(repo, { running: false });
}

export async function stopRequested(repo) {
  try {
    await fs.access(path.join(forgeDir(repo), "STOP"));
    return true;
  } catch {
    return false;
  }
}

async function writeIfMissing(filePath, content) {
  try {
    await fs.access(filePath);
  } catch {
    await fs.mkdir(path.dirname(filePath), { recursive: true });
    await fs.writeFile(filePath, content);
  }
}
