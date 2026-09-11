# External trigger: cron-job.org → GitHub Actions

GitHub's own cron is a best-effort queue. Through August 2026 it fired 1–3h
late, and on Aug 27–28 every slot fired 10+ hours late (after the close), so
two trading days had no runs at all. cron-job.org (free) calls the workflow's
dispatch endpoint at the exact minute instead. The bot code is unchanged; only
the alarm clock moves. GitHub's crons stay on as a trimmed fallback.

## 1. Create the GitHub token (you do this — never paste it anywhere but cron-job.org)

github.com → your avatar → **Settings** → **Developer settings** →
**Personal access tokens** → **Fine-grained tokens** → **Generate new token**

| Field | Value |
|---|---|
| Token name | `cron-job-org trading-bot` |
| Expiration | 1 year (put a calendar reminder to rotate it) |
| Resource owner | `tobiasceg` |
| Repository access | **Only select repositories** → `algotrade` |
| Repository permissions | **Actions: Read and write** (Metadata: Read is added automatically) |

Nothing else. Copy the token once; GitHub will not show it again.

## 2. Create four jobs on cron-job.org

Sign up at https://cron-job.org (free). For each job: **Create cronjob**.

Every job uses the same request; only the schedule and the body differ.

**URL**
```
https://api.github.com/repos/tobiasceg/algotrade/actions/workflows/trading-bot.yml/dispatches
```

**Advanced → Request method:** `POST`

**Advanced → Headers** (four of them):
```
Accept: application/vnd.github+json
Authorization: Bearer <paste the token here>
X-GitHub-Api-Version: 2022-11-28
Content-Type: application/json
```

**Schedule → timezone: UTC** (not Singapore, not New York — see "Why UTC" below).

| Job title | Schedule (UTC, every day) | Request body |
|---|---|---|
| `bot entry` | 13:45 | `{"ref":"main","inputs":{"mode":"entry","cron":"45 13 * * *"}}` |
| `bot entry backup` | 14:30 | `{"ref":"main","inputs":{"mode":"entry","cron":"30 14 * * *"}}` |
| `bot exit` | 15:00 | `{"ref":"main","inputs":{"mode":"exit","cron":"0 15 * * *"}}` |
| `bot exit backup` | 18:05 | `{"ref":"main","inputs":{"mode":"exit","cron":"5 18 * * *"}}` |

The `cron` input is only telemetry: it tells the run which slot fired it, so
closed-day timing probes keep working. It must match the schedule you set.

Turn on **Save responses** and **failure notifications** for each job.

## 3. Test one

Open `bot entry`, click **Run now** (or "Execute now"). A successful call
returns **HTTP 204** with an empty body. Within about a minute a run appears at
https://github.com/tobiasceg/algotrade/actions with event `workflow_dispatch`.
On a weekend it will log a timing probe and stop; on a trading day it will
sleep to 10:00 ET and do the normal entry work.

If you get **401** the token is wrong or expired; **404** usually means the
token lacks Actions write on this repo; **422** means the body is malformed.

## Why UTC, and why these times

main.py sleeps from whenever it starts until the real work time (entry
10:00 ET, exit 1h45m before the close). Scheduling in UTC means the jobs land
at slightly different ET times in summer and winter, and the sleep absorbs it:

| Job | Summer (ET) | Winter (ET) | What happens |
|---|---|---|---|
| entry 13:45 UTC | 09:45 | 08:45 | sleeps to 10:00 |
| entry backup 14:30 UTC | 10:30 | 09:30 | runs at once / sleeps to 10:00 |
| exit 15:00 UTC | 11:00 | 10:00 | sleeps to 14:15 (11:15 on half days) |
| exit backup 18:05 UTC | 14:05 | 13:05 | sleeps to 14:15 |

Every sleep is under the 5h cap, and the exit primary fires early enough to
cover 13:00 half-day closes. The backups are redundancy: the journal dedupe
makes the second arrival skip.

## What changes for you

Nothing in the Telegram messages. The runs will simply start on time. If
cron-job.org ever stops calling, GitHub's fallback crons still fire (late) and
the closed-day probes in the journal will show which trigger is doing the work.
