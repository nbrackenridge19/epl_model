// Pure scheduling logic for the EPL trigger Worker (no Cloudflare APIs in here, so it can be unit tested).
//
// One match -> four timed jobs, all measured back from kickoff:
//
//   lineup_primary       T-50  starts the lineup job, which polls ESPN every 5 min until both lineups are posted
//   odds_lineup_release  T-50  captures DraftKings odds, stored as line_type 'lineup_release' (the line bets use)
//   lineup_safety        T-25  second lineup run; GitHub queues it behind the primary and it exits at once if the
//                              primary already captured both lineups (only does real work if the primary crashed)
//   odds_closing         T-5   captures DraftKings odds, stored as line_type 'closing' (kept for the data model)

export const MIN = 60 * 1000;
export const MAX_RETRIES = 2;
export const RETRY_DELAY_MS = 30 * 1000;

export type JobStatus = "pending" | "done" | "gave_up" | "skipped_late";

export interface Job {
  key: string;
  event_type: string;
  payload: Record<string, string>;
  at_ms: number; // when the job should fire
  expires_ms: number; // after this it is no longer worth firing
  status: JobStatus;
  retries: number;
  next_ms: number | null; // set while waiting to retry a failed dispatch
  outcome: string | null;
  last_http: number | null;
  fired_at: string | null;
}

export function planJobs(matchId: string, kickoffMs: number): Job[] {
  const base = { status: "pending" as JobStatus, retries: 0, next_ms: null, outcome: null, last_http: null, fired_at: null };
  return [
    {
      ...base,
      key: "lineup_primary",
      event_type: "epl-lineup",
      payload: { match_id: matchId, attempt: "primary" },
      at_ms: kickoffMs - 50 * MIN,
      expires_ms: kickoffMs + 10 * MIN,
    },
    {
      ...base,
      key: "odds_lineup_release",
      event_type: "epl-odds",
      payload: { match_id: matchId, line_type: "lineup_release" },
      at_ms: kickoffMs - 50 * MIN,
      expires_ms: kickoffMs - 10 * MIN,
    },
    {
      ...base,
      key: "lineup_safety",
      event_type: "epl-lineup",
      payload: { match_id: matchId, attempt: "safety" },
      at_ms: kickoffMs - 25 * MIN,
      expires_ms: kickoffMs + 10 * MIN,
    },
    {
      ...base,
      key: "odds_closing",
      event_type: "epl-odds",
      payload: { match_id: matchId, line_type: "closing" },
      at_ms: kickoffMs - 5 * MIN,
      expires_ms: kickoffMs,
    },
  ];
}

// Called every time the scheduler re-sends a match. Same kickoff time -> keep every job's state exactly as it is
// (so a job that already fired never fires again). Different kickoff time -> the match moved, start fresh.
export function reconcile(
  existingJobs: Job[] | undefined,
  existingKickoffIso: string | undefined,
  newKickoffIso: string,
  matchId: string,
): { jobs: Job[]; changed: boolean } {
  if (existingJobs && existingJobs.length > 0 && existingKickoffIso === newKickoffIso) {
    return { jobs: existingJobs, changed: false };
  }
  return { jobs: planJobs(matchId, Date.parse(newKickoffIso)), changed: true };
}

export function fireTime(job: Job): number {
  return job.next_ms ?? job.at_ms;
}

// Marks pending jobs that can no longer usefully fire. Returns true if anything changed.
export function expireLate(jobs: Job[], now: number): boolean {
  let changed = false;
  for (const job of jobs) {
    if (job.status === "pending" && now > job.expires_ms) {
      job.status = "skipped_late";
      job.outcome = job.retries > 0 ? `expired after ${job.retries} failed dispatch retries` : "expired before it could fire";
      changed = true;
    }
  }
  return changed;
}

export function dueJobs(jobs: Job[], now: number): Job[] {
  return jobs.filter((j) => j.status === "pending" && fireTime(j) <= now).sort((a, b) => fireTime(a) - fireTime(b));
}

export function nextAlarmMs(jobs: Job[]): number | null {
  const pending = jobs.filter((j) => j.status === "pending");
  if (pending.length === 0) return null;
  return Math.min(...pending.map(fireTime));
}

// GitHub answers a successful repository_dispatch with HTTP 204.
export function applyDispatchResult(job: Job, httpStatus: number, now: number): void {
  job.last_http = httpStatus;
  job.fired_at = new Date(now).toISOString();
  if (httpStatus === 204) {
    job.status = "done";
    job.next_ms = null;
    job.outcome = job.retries === 0 ? "ok" : `ok after ${job.retries} retr${job.retries === 1 ? "y" : "ies"}`;
    return;
  }
  if (job.retries < MAX_RETRIES) {
    job.retries += 1;
    job.next_ms = now + RETRY_DELAY_MS;
    job.outcome = `HTTP ${httpStatus}, retry ${job.retries}/${MAX_RETRIES} pending`;
    return;
  }
  job.status = "gave_up";
  job.next_ms = null;
  job.outcome = `GAVE UP after ${MAX_RETRIES} retries (last HTTP ${httpStatus})`;
}
