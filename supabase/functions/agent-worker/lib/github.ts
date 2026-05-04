const REPO = "rrmethodco/method-q1-2026-dashboards";

function ghToken(): string {
  const t = Deno.env.get("GITHUB_PAT");
  if (!t) throw new Error("GITHUB_PAT secret not set");
  return t;
}

async function ghFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const url = `https://api.github.com${path}`;
  return fetch(url, {
    ...init,
    headers: {
      ...(init.headers || {}),
      Authorization: `Bearer ${ghToken()}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
    },
  });
}

export interface WorkflowRun {
  id: number;
  name: string;
  status: string;
  conclusion: string | null;
  created_at: string;
  workflow_id: number;
  head_branch: string;
}

export async function listRecentRuns(workflowFile: string, limit = 5): Promise<WorkflowRun[]> {
  const r = await ghFetch(
    `/repos/${REPO}/actions/workflows/${workflowFile}/runs?per_page=${limit}`,
  );
  if (!r.ok) throw new Error(`gh ${r.status}: ${await r.text()}`);
  const body = await r.json();
  return body.workflow_runs || [];
}

export async function dispatchWorkflow(
  workflowFile: string,
  ref = "main",
  inputs: Record<string, string> = {},
): Promise<void> {
  const r = await ghFetch(
    `/repos/${REPO}/actions/workflows/${workflowFile}/dispatches`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ref, inputs }),
    },
  );
  if (!r.ok) throw new Error(`dispatch ${r.status}: ${await r.text()}`);
}
