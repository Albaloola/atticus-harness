import fs from "node:fs/promises";
import path from "node:path";
import { runCommand } from "./git.mjs";

export async function harvestTasks(repo, policy) {
  const fromBacklog = await backlogTasks(repo, policy);
  if (fromBacklog.length > 0) return fromBacklog;
  const fromTodos = await todoTasks(repo, policy);
  if (fromTodos.length > 0) return fromTodos;
  return [
    {
      id: "T-0001",
      title: "Document the next safe Forge improvement",
      reason: "No backlog or TODO tasks were found.",
      risk: "low",
      value: "medium",
      estimatedDiffLines: 40,
      allowedPaths: ["FORGE_BACKLOG.md"],
      forbiddenPaths: policy.forbiddenPaths,
      requiredChecks: [],
      successCriteria: ["FORGE_BACKLOG.md contains a concrete future task."],
      score: 1,
    },
  ];
}

export function selectTask(tasks) {
  return [...tasks].sort((a, b) => (b.score ?? 0) - (a.score ?? 0))[0];
}

export function builderPrompt(task) {
  return `# Role\n\nYou are the builder agent inside Forge. You are working inside an isolated git worktree. Complete exactly one task.\n\n# Task\n\n\`\`\`json\n${JSON.stringify(task, null, 2)}\n\`\`\`\n\n# Rules\n\n1. Only modify allowed paths.\n2. Do not modify forbidden paths.\n3. Keep the diff small.\n4. Add or update tests when possible.\n5. Do not disable tests.\n6. Do not remove safety checks.\n7. Do not touch secrets or environment files.\n8. Do not make external network calls.\n9. Do not auto-merge or push.\n10. Stop after the task is complete.\n`;
}

async function backlogTasks(repo, policy) {
  const filePath = path.join(repo, "FORGE_BACKLOG.md");
  let text = "";
  try {
    text = await fs.readFile(filePath, "utf8");
  } catch {
    return [];
  }
  const tasks = [];
  for (const [index, line] of text.split("\n").entries()) {
    const match = line.match(/^\s*-\s+\[ \]\s+(.+?)\s*$/);
    if (!match) continue;
    tasks.push({
      id: `T-${String(tasks.length + 1).padStart(4, "0")}`,
      title: match[1],
      reason: `Operator backlog item from FORGE_BACKLOG.md line ${index + 1}.`,
      risk: "low",
      value: "high",
      estimatedDiffLines: 200,
      allowedPaths: ["FORGE_BACKLOG.md", "forge-app/", "forge/", "tests/", "README.md", "docs/"],
      forbiddenPaths: policy.forbiddenPaths,
      requiredChecks: policy.requiredChecks,
      successCriteria: ["The backlog item is addressed with the smallest useful diff.", "Required checks pass."],
      score: 8,
    });
  }
  return tasks;
}

async function todoTasks(repo, policy) {
  const result = await runCommand("git", ["grep", "-n", "-E", "TODO|FIXME|HACK|XXX"], {
    cwd: repo,
    timeoutMs: 30_000,
  });
  if (![0, 1].includes(result.exitCode)) return [];
  return result.stdout
    .split("\n")
    .filter(Boolean)
    .slice(0, 10)
    .map((line, index) => {
      const [file, lineNo, ...rest] = line.split(":");
      const parent = path.dirname(file).replace(/^\.$/, "");
      return {
        id: `T-${String(index + 1).padStart(4, "0")}`,
        title: `Address TODO in ${file}`,
        reason: `Found maintenance marker at ${file}:${lineNo}: ${rest.join(":").slice(0, 160)}`,
        risk: "low",
        value: "medium",
        estimatedDiffLines: 160,
        allowedPaths: [file, parent ? `${parent}/` : file, "tests/"],
        forbiddenPaths: policy.forbiddenPaths,
        requiredChecks: policy.requiredChecks,
        successCriteria: ["The marker is resolved or clarified.", "Required checks pass."],
        score: 6,
      };
    });
}
