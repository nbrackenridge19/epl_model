// EPL trigger Worker.
//
// Role: fire GitHub Actions workflows at exact times relative to each match's kickoff. The Worker does no scraping and
// has no database access; it only holds timers (one Durable Object per match) and calls GitHub's repository_dispatch.
//
// How it fits together:
//   1. A Cloudflare cron trigger (wrangler.toml) fires the "epl-schedule" event every 30 minutes. That runs
//      .github/workflows/epl_scheduler.yml, which reads kickoff times from the database and POSTs each one to
//      /match/{match_id}/schedule on this Worker.
//   2. The match's Durable Object stores four timed jobs (see plan.ts) and keeps a single alarm set to the earliest one.
//   3. When an alarm fires, the Durable Object sends the job's repository_dispatch event ("epl-lineup" or "epl-odds")
//      to GitHub, retrying a failed dispatch twice, 30 seconds apart.
//
// Endpoints (every one needs the header "Authorization: Bearer <SCHEDULE_SECRET>"):
//   POST /schedule-now                  run the scheduler workflow now instead of waiting for the next cron tick
//   POST /match/{match_id}/schedule     body {"kickoff_iso": "2026-10-10T14:00:00Z"}  (re)sets that match's timers
//   GET  /match/{match_id}/status       what is scheduled for that match and how each job went
//   POST /match/{match_id}/trigger-now  body {"job": "lineup_primary"}  fires one job immediately, for recovery/testing
//                                       (job: lineup_primary | odds_lineup_release | lineup_safety | odds_closing)

import {
  Job,
  applyDispatchResult,
  dueJobs,
  expireLate,
  nextAlarmMs,
  planJobs,
  reconcile,
} from "./plan";

interface Env {
  MATCH_ALARM: DurableObjectNamespace;
  GITHUB_TOKEN: string;
  GITHUB_OWNER: string;
  GITHUB_REPO: string;
  SCHEDULE_SECRET: string;
}

const MATCH_ID_RE = /^[0-9a-fA-F-]{36}$/;

async function postGithubDispatch(env: Env, event_type: string, client_payload: Record<string, string>) {
  let status = -1;
  let body = "";
  const url = `https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}/dispatches`;
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "epl-trigger-worker",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ event_type, client_payload }),
    });
    status = res.status;
    body = await res.text();
  } catch (err) {
    body = String(err);
  }
  console.log(`dispatch ${event_type} ${JSON.stringify(client_payload)}: HTTP ${status} ${body}`);
  return { status, body };
}

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
}

function summarize(jobs: Job[]) {
  return jobs.map((j) => ({
    job: j.key,
    event: j.event_type,
    fire_at: new Date(j.at_ms).toISOString(),
    status: j.status,
    retries: j.retries,
    outcome: j.outcome,
    last_http: j.last_http,
    fired_at: j.fired_at,
  }));
}

export class MatchAlarm {
  state: DurableObjectState;
  env: Env;

  constructor(state: DurableObjectState, env: Env) {
    this.state = state;
    this.env = env;
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    const matchId = url.searchParams.get("match_id") || "";

    if (request.method === "POST" && url.pathname === "/schedule") {
      let body: { kickoff_iso?: string } = {};
      try {
        body = await request.json();
      } catch {
        return json({ error: "body must be JSON" }, 400);
      }
      const kickoffMs = Date.parse(body.kickoff_iso || "");
      if (Number.isNaN(kickoffMs)) return json({ error: "bad kickoff_iso" }, 400);

      const existingJobs = await this.state.storage.get<Job[]>("jobs");
      const existingKickoff = await this.state.storage.get<string>("kickoff_iso");
      const { jobs, changed } = reconcile(existingJobs, existingKickoff, body.kickoff_iso as string, matchId);

      expireLate(jobs, Date.now());
      await this.state.storage.put("match_id", matchId);
      await this.state.storage.put("kickoff_iso", body.kickoff_iso as string);
      await this.state.storage.put("jobs", jobs);
      await this.armAlarm(jobs);
      return json({ ok: true, match_id: matchId, kickoff_iso: body.kickoff_iso, rescheduled: changed, jobs: summarize(jobs) });
    }

    if (request.method === "GET" && url.pathname === "/status") {
      const jobs = (await this.state.storage.get<Job[]>("jobs")) || [];
      const alarm = await this.state.storage.getAlarm();
      return json({
        match_id: (await this.state.storage.get<string>("match_id")) || matchId,
        kickoff_iso: (await this.state.storage.get<string>("kickoff_iso")) || null,
        next_alarm: alarm ? new Date(alarm).toISOString() : null,
        jobs: summarize(jobs),
      });
    }

    if (request.method === "POST" && url.pathname === "/trigger-now") {
      let which = "lineup_primary";
      try {
        const body = (await request.json()) as { job?: string };
        if (body && body.job) which = body.job;
      } catch {
        // no body -> default job
      }
      const template = planJobs(matchId, Date.now() + 60 * 60 * 1000).find((j) => j.key === which);
      if (!template) return json({ error: `unknown job '${which}'` }, 400);
      const result = await postGithubDispatch(this.env, template.event_type, template.payload);
      return json({ job: which, ...result });
    }

    return new Response("not found", { status: 404 });
  }

  async alarm(): Promise<void> {
    try {
      const jobs = (await this.state.storage.get<Job[]>("jobs")) || [];
      expireLate(jobs, Date.now());
      for (const job of dueJobs(jobs, Date.now())) {
        console.log(`ALARM: dispatching ${job.key} for match ${job.payload.match_id}`);
        const result = await postGithubDispatch(this.env, job.event_type, job.payload);
        applyDispatchResult(job, result.status, Date.now());
        // Save after every job so an interruption can never cause an already-sent job to be sent twice.
        await this.state.storage.put("jobs", jobs);
      }
      await this.state.storage.put("jobs", jobs);
      await this.armAlarm(jobs);
    } catch (err) {
      // Never let an exception escape: Cloudflare would re-run the alarm automatically.
      console.log(`alarm error: ${String(err)}`);
      const jobs = (await this.state.storage.get<Job[]>("jobs")) || [];
      await this.armAlarm(jobs);
    }
  }

  async armAlarm(jobs: Job[]): Promise<void> {
    const next = nextAlarmMs(jobs);
    if (next === null) {
      await this.state.storage.deleteAlarm();
      return;
    }
    // An alarm set in the past fires right away, which is the intended catch-up behaviour.
    await this.state.storage.setAlarm(Math.max(next, Date.now() + 1000));
  }
}

function authorized(request: Request, env: Env): boolean {
  const auth = request.headers.get("Authorization") || "";
  const expected = `Bearer ${env.SCHEDULE_SECRET}`;
  if (!env.SCHEDULE_SECRET || auth.length !== expected.length) return false;
  let diff = 0;
  for (let i = 0; i < expected.length; i++) diff |= auth.charCodeAt(i) ^ expected.charCodeAt(i);
  return diff === 0;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (!authorized(request, env)) return new Response("unauthorized", { status: 401 });

    const url = new URL(request.url);
    if (request.method === "POST" && url.pathname === "/schedule-now") {
      return json(await postGithubDispatch(env, "epl-schedule", {}));
    }

    const m = url.pathname.match(/^\/match\/([^/]+)(\/.*)?$/);
    if (!m || !MATCH_ID_RE.test(m[1])) return new Response("not found", { status: 404 });

    const stub = env.MATCH_ALARM.get(env.MATCH_ALARM.idFromName(m[1]));
    const doUrl = new URL(request.url);
    doUrl.pathname = m[2] || "/status";
    doUrl.searchParams.set("match_id", m[1]);
    return stub.fetch(new Request(doUrl.toString(), request));
  },

  // Cloudflare cron trigger (schedule is in wrangler.toml): runs epl_scheduler.yml through repository_dispatch.
  async scheduled(_event: ScheduledEvent, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(postGithubDispatch(env, "epl-schedule", {}));
  },
};
