# Meet Intelligence

Automatically joins Google Meet calls via a server-side bot, records them, transcribes
them (multilingual, Indian languages + Hinglish), and runs sentiment analysis +
summarization.

No Google Workspace / Meet / Drive APIs are used anywhere in this system. Capture is
done by a headless-Chrome bot joining as a normal participant.

This README grows one section per phase.

---

## Phase 1 — Foundation

**What's built:**
- `docker-compose.yml` with Postgres 16, Redis 7, an S3-compatible `storage` service,
  and the FastAPI `api` service.
- `common/` — shared config (`config.py`), structured JSON logging (`logging.py`),
  SQLAlchemy engine/session (`db.py`), ORM models for the full schema (`models.py`),
  and an S3/MinIO storage client (`storage.py`).
- `migrations/` — Alembic, with an initial migration creating all five tables from the
  spec (`meetings`, `participants`, `transcript_segments`, `segment_sentiment`,
  `meeting_analysis`). Only `meetings` is exposed via the API so far; the rest exist now
  so later phases are pure application code, no schema churn.
- `api/` — FastAPI app with:
  - `POST /meetings` — create a meeting (`meet_url`, `scheduled_at`, `expected_languages`).
    Rejects URLs that aren't `meet.google.com` links.
  - `GET /meetings/{id}` — fetch one meeting (404 if missing).
  - `GET /meetings` — list meetings, optional `?status=` filter, paginated.
  - `GET /health` — liveness check.
- `tests/` — pytest + FastAPI `TestClient`, against a real Postgres (no SQLite —
  the schema uses Postgres-specific `ARRAY`/`JSONB` types).

**What was removed:** the repo previously had `main.py` / `test_transcripts.py` using
OAuth + the Google Meet REST API directly. That contradicts this project's no-Meet-API
constraint, so those files (and `credentials.json` / `token.json`) were deleted per your
confirmation.

**Design notes / deviations from the spec you should know about:**
- `status` and `role` are plain `String` columns (validated at the Pydantic/API layer),
  not native Postgres `ENUM` types — makes it much cheaper to add a new status later
  (no `ALTER TYPE`), at the cost of DB-level enforcement. Can switch to native enums
  later if you'd rather have the DB enforce it.
- `segment_sentiment` and `meeting_analysis` each got their own surrogate `id` (the
  spec's sketch didn't have one) so a segment/meeting could in principle be re-scored
  by a newer `model_version` without an awkward composite key.
- Primary keys are client-generated UUIDv4 (`default=uuid.uuid4` in the ORM), not
  `gen_random_uuid()` server-side, so no `pgcrypto` extension is required.
- **MinIO's own Docker image now requires a paid Docker Hub subscription to pull**
  (confirmed while building this — `docker pull minio/minio` returns "pull access
  denied" even anonymously logged out, same for `quay.io/minio/minio`). Local dev
  instead runs `zenko/cloudserver`, a free, open-source server implementing the same
  S3 API. All code (`common/storage.py`) talks to it purely through `boto3`'s generic
  S3 client, so nothing changes if you later point `MINIO_ENDPOINT` at real MinIO (with
  a license), AWS S3, or Cloudflare R2 — same env vars, same code path.
  `docker/zenko-config.json` is CloudServer's default config with one addition: the
  `storage` hostname (the compose service name) added to `restEndpoints`, otherwise it
  rejects requests whose `Host` header it doesn't recognize.

### Setup

1. Copy the env template (already done for you — `.env` exists with throwaway local
   dev defaults; edit if you want different ports/credentials):
   ```
   cp .env.example .env   # only needed if you deleted .env
   ```
2. Start everything:
   ```
   docker compose up --build
   ```
   This starts Postgres, Redis, the S3-compatible storage service, and the API. The
   `api` container runs `alembic upgrade head` automatically before starting `uvicorn`.
3. API is at `http://localhost:8000` (docs at `/docs`). Object storage's S3 API is at
   `http://localhost:9000` (default keys `accessKey1` / `verySecretKey1`, see `.env`).

### Try it

```bash
curl -X POST localhost:8000/meetings \
  -H 'content-type: application/json' \
  -d '{"meet_url":"https://meet.google.com/abc-defg-hij","scheduled_at":"2026-10-15T10:00:00Z","expected_languages":["hi","en"]}'

curl localhost:8000/meetings
curl localhost:8000/meetings/<id-from-above>
```

### Tests

Tests run against a real Postgres (not mocked) because the schema uses Postgres-only
column types.

```bash
docker compose up -d postgres redis storage
python -m venv .venv && source .venv/bin/activate   # if you don't already have one
pip install -r requirements.txt
pytest
```

`tests/conftest.py` creates all tables directly via SQLAlchemy metadata (not through
Alembic) in a session-scoped fixture, and truncates them between tests — fast, and
independent of migration history. It runs against a dedicated `<your db>_test`
database (created automatically on first run) so it never touches, and can never
`drop_all` on, the dev database the `api` container's `alembic upgrade head` manages —
an earlier version of this fixture didn't do that and wiped the dev schema; keep the
`_test`-suffixed database if you change how tests are wired.

### Open questions / risks for you

1. **Status/role as strings vs native enums** — fine with the tradeoff above, or would
   you rather enforce these at the DB level with real Postgres enums?
2. **This directory isn't a git repo yet.** `.gitignore` existed already so it looks
   like you intended to version this — want me to `git init` and make an initial commit
   once you're happy with Phase 1, or are you managing that yourself?
3. **Local Python version**: your `.venv` is Python 3.13, but the spec pins the stack
   to 3.11 (and the Docker image I wrote uses `python:3.11-slim`). Nothing in Phase 1
   needs 3.11-only features, so I left your venv alone — flagging it now because later
   phases (pyannote, Playwright, torch/transformers) are far more likely to care about
   exact Python/CUDA compatibility. Want a 3.11 venv for local dev too, or is relying on
   Docker for the real runtime enough?
4. No auth on the API yet — intentional, per the spec (Phase 7 adds basic auth).
5. **`zenko/cloudserver` has no native arm64 image** — on Apple Silicon, Docker runs it
   emulated (amd64 under Rosetta), which Docker reported explicitly. It worked fine in
   testing, but it's slower than a native image and worth knowing about if `storage`
   ever looks sluggish. Not a concern on amd64 hosts, or in any real cloud deployment
   (which should use real S3/R2/MinIO anyway, not this dev stand-in).

---

## Change request — Google Calendar trigger + concurrent meetings

Replaces the planned Cal.com webhook (never built, so nothing to delete in code) with
Google Calendar as the booking trigger, and adds the infrastructure for running many
bots at once. Still ahead: the actual bot container (Phase 2) — this change request
builds the calendar sync engine and the bot account pool it will check out from.

**What's built:**
- `common/models.py` — `Meeting` gained `google_event_id`, `google_ical_uid` (unique),
  `google_etag`, `owner_email`, plus two new statuses (`cancelled`, `no_bot_available`).
  New tables `calendar_sync_state` and `bot_accounts`.
- `migrations/versions/0002_calendar_and_bot_pool.py` — additive migration for all of
  the above; applied cleanly on top of `0001` with no data loss.
- `calendar_sync/` —
  - `auth.py`: `CalendarAuthProvider` interface, `ServiceAccountDWDProvider` (domain-wide
    delegation, impersonates each employee — the prod path) and
    `OAuthRefreshTokenProvider` (one stored refresh token — local dev / single-user).
    Selected by `CALENDAR_AUTH_MODE`. Scope is `calendar.readonly` unless
    `AUTO_ADD_BOT_GUEST=true`, which needs `calendar.events`.
  - `mapping.py`: pure functions — Meet URL from `hangoutLink` or a `video`
    `conferenceData` entry point; `Language: hi, en` parsing from the description
    (case-insensitive); event title prefix filter.
  - `sync.py`: the actual sync engine. Incremental via `syncToken`; on HTTP 410 (token
    expired/invalid) it clears the token and does one full resync automatically.
    Handles create / update (etag-diffed; a changed start time calls
    `bot_jobs.reschedule_job`) / cancel (`bot_jobs.cancel_job` + releases any reserved
    bot account). Dedups on `iCalUID`, falling back to Meet URL — a second employee's
    calendar for the same event adds them as an `agent` participant instead of a new
    meeting, so the same meeting never gets two bots.
  - `poller.py`: the only part that actually sleeps-and-loops (every
    `CALENDAR_POLL_SECONDS`), kept separate from `sync.py` on purpose — swapping this
    for `events.watch` push notifications later won't touch the sync logic.
  - `guests.py`: `AUTO_ADD_BOT_GUEST=true` path — reserves a bot account for the
    meeting's window, then patches *only* the attendee list onto the event.
- `bot_pool/` — see the reservation-redesign update below for the current (table-based)
  design; `cli.py` (`python -m bot_pool.cli add|list|disable|enable`) is unchanged.
- `scheduler/main.py` — new long-running container: runs the calendar poller and the
  bot-pool reaper as two background loops. Phase 2 will add a third loop here to
  actually launch bot containers.
- `common/bot_jobs.py` — `schedule_job` / `reschedule_job` / `cancel_job`: logging
  stubs that `calendar_sync` calls today. Phase 2 replaces their bodies with real
  container scheduling; nothing that calls them needs to change.
- API: `POST /bot-accounts`, `GET /bot-accounts` (optional `?status=`),
  `PATCH /bot-accounts/{id}` for admin management from outside the CLI/dashboard.
  `MeetingRead` now includes the new Google fields.

**Deviations / additions beyond what was asked, worth knowing about:**
- Added `GOOGLE_OAUTH_TOKEN_FILE`, `BOT_RESERVATION_LEAD_SECONDS` (default 120, i.e. the
  spec's "2 min"), and `BOT_RESERVATION_TRAIL_SECONDS` (default 1800, i.e. "30 min") —
  implied by the spec's reservation window but not explicitly named as env vars.
- `bot_accounts.status`, like `meetings.status`, is a plain `String` column, not a
  native enum — same tradeoff as Phase 1, for the same reason.
- Added bot-account admin HTTP endpoints alongside the CLI the spec asked for, since
  the dashboard (Phase 6) will want them anyway.

**What's intentionally NOT done here (Phase 2 territory):**
- Actually launching one bot container per meeting, the "one bot per meeting" isolation
  (own Xvfb/PulseAudio/ffmpeg), and heartbeats being sent *from* a running bot.
  `bot_pool.service` is fully built and tested; nothing calls `checkout_account` yet
  outside of tests.
- Running a GPU worker — see "Running multiple GPU workers" below for the convention
  this change request commits to, but Celery/the workers themselves start in Phase 3.
- Testing two overlapping meetings in two real containers with separate recordings
  (one of the items in the change request's test list) — can't be meaningfully tested
  until Phase 2 exists. The pool's concurrency safety *underneath* that scenario is
  tested now (see Tests).

### Setup

**Google Cloud project** (one-time, for either auth mode):
1. In the [Google Cloud Console](https://console.cloud.google.com/), create (or reuse) a
   project, then enable the **Google Calendar API** under "APIs & Services" (not Meet,
   not Drive — this system never calls those).
2. For `CALENDAR_AUTH_MODE=oauth` (local dev / single user): under "APIs & Services >
   Credentials", create an OAuth client ID of type "Desktop app", download the JSON, and
   point `GOOGLE_OAUTH_CLIENT_FILE` at it. The first calendar sync opens a browser
   consent screen once; the refresh token is cached to `GOOGLE_OAUTH_TOKEN_FILE`
   afterwards.
3. For `CALENDAR_AUTH_MODE=service_account` (prod / multiple employees), see domain-wide
   delegation below instead — you don't need the OAuth client for this mode.

**Domain-wide delegation** (Google Workspace Admin console, `service_account` mode only):
1. In Cloud Console, create a service account and download its JSON key; point
   `GOOGLE_SERVICE_ACCOUNT_FILE` at it.
2. Copy the service account's **Client ID** (a numeric OAuth2 client ID, found on the
   service account's "Details" tab — not its email).
3. As a Workspace super admin, go to **admin.google.com > Security > API Controls >
   Domain-wide Delegation**, click "Add new", paste the Client ID, and grant exactly this
   OAuth scope: `https://www.googleapis.com/auth/calendar.readonly` (add
   `.../calendar.events` too only if you're turning on `AUTO_ADD_BOT_GUEST`).
4. Set `EMPLOYEE_EMAILS` to the comma-separated list of employees whose calendars to
   watch — the service account impersonates each one in turn via `with_subject()`. No
   per-employee consent screen is needed once delegation is granted.
5. Set `CALENDAR_ID` (defaults to `primary`) — use a non-default value if you're watching
   one shared appointments calendar instead of each employee's own.
6. Optionally set `EVENT_TITLE_PREFIX` to only process events whose title starts with it.

**Bot account setup** (one Gmail account + Chrome profile per pool slot):
1. Create (or obtain) a Gmail account dedicated to the bot — never reuse an employee's
   own account, since the profile is mounted into a container and the account's
   recording-notice display name is set from this identity.
2. Decide its profile path, e.g. `./bot-profiles/bot1` (Phase 2 mounts this into the bot
   container as a persistent Chrome profile — create the directory now so Phase 2's
   volume mount has somewhere to point).
3. Manually log that Gmail account into Chrome once using that same profile directory
   (Phase 2's README section will give the exact `docker run` invocation once the bot
   image exists — for now, any Chrome instance pointed at that user-data-dir works) so
   the session persists.
4. Register it in the pool:
   ```bash
   python -m bot_pool.cli add --email bot1@gmail.com --profile-path ./bot-profiles/bot1
   python -m bot_pool.cli list
   ```
   Or via the API: `POST /bot-accounts {"email": "...", "profile_path": "..."}`.
5. Repeat per concurrent-meeting slot you want capacity for. `disable`/`enable` via the
   CLI or `PATCH /bot-accounts/{id}` take an account out of/back into rotation (e.g. for
   maintenance or a flagged/suspended Gmail account) without deleting its row.

**Running it:**
```bash
docker compose up --build
```
now also starts a `scheduler` container (calendar poller + bot-pool reaper). If
`EMPLOYEE_EMAILS` is empty it logs a warning and the poller loop is a no-op — safe to
leave running with no calendar configured yet.

**Running multiple GPU workers** (convention now, Celery itself arrives in Phase 3):
the spec calls for a dedicated Celery queue named `gpu` for diarization/ASR tasks, with
jobs required to be idempotent so re-running one from a retry or a second worker never
duplicates rows. That convention is committed to here so Phase 3+ doesn't have to
retrofit it: processing tasks will be written as upserts keyed on
`(meeting_id, start_ms, end_ms)` rather than blind inserts, and `celery -A workers worker
-Q gpu --concurrency=1` (one process per GPU) is how you'll scale to N GPU machines —
just point each at the same `REDIS_URL` and Postgres. Nothing to run yet.

### Tests

`tests/test_calendar_mapping.py` (pure, no DB/network), `tests/test_calendar_sync.py`
and `tests/test_calendar_guests.py` (use a fake Google Calendar client —
`tests/fakes.py` — shaped like the real `googleapiclient` service, so no credentials
needed), and `tests/test_bot_pool.py` (against the real Postgres, like everything else
here).

Covers every item in the change request's test list except the one that needs a real
bot container (see "intentionally NOT done" above):
- incremental sync (uses stored `syncToken`) and full sync (none yet / 410 Gone clears it
  and resyncs)
- `EVENT_TITLE_PREFIX` filtering
- Meet URL extraction (both `hangoutLink` and `conferenceData` entry points — in
  `test_calendar_mapping.py`) and `Language:` parsing
- same `iCalUID` on two calendars -> one meeting, two participants (host + agent)
- update changes fields and calls `reschedule_job` when the start time moved; unchanged
  etag is a no-op
- cancel sets `status=cancelled`, calls `cancel_job`, releases any reserved bot account
- concurrent checkout across real threads/connections never double-assigns an account
  (`test_concurrent_checkout_never_double_assigns_an_account` — 10 threads racing for 3
  accounts, asserted no account id appears twice)
- reaper releases a stale heartbeat and fails its meeting; ignores a fresh one
- exhausted pool sets `no_bot_available`

```bash
pytest   # same as Phase 1 — needs postgres/redis/storage up
```

### Resolved from the first round

Reservation window math confirmed (`LEAD=120`/`TAIL=1800`, see below for the env var
rename), and the single-reservation-per-account design and its reschedule bug are fixed
in the update below. Dev smoke-test rows are cleared. The remaining open item — no real
Google credentials exercised yet — is addressed by the live preview CLI below, which you
can run once the service account file is in place.

---

## Update — bot reservations redesign, client extraction, live config

**Why:** storing `reserved_from`/`reserved_until` directly on `bot_accounts` only
allowed *one* future reservation per account — wrong, since an account must be
bookable for many non-overlapping meetings. This replaces that with a proper
reservations table and fixes the reschedule bug flagged in the first round.

**What changed:**
- **`bot_accounts`** lost `reserved_from`/`reserved_until`. `status` is now `free` |
  `in_use` | `disabled` — **live state only** (is a bot running as this account right
  now?), completely independent of future bookings.
- **New table `bot_reservations`** (migration `0003`): `bot_account_id`, `meeting_id`
  (unique), a `tstzrange` `window`, and a Postgres **`EXCLUDE USING gist`** constraint
  (`bot_account_id WITH =, window WITH &&`, needs the `btree_gist` extension — the
  migration creates it) that makes two overlapping windows on the same account
  physically impossible to insert, not just app-logic-prevented. `test_overlap_rejected_at_db_level`
  proves this directly, bypassing all application code.
- **`bot_pool/service.py` rewritten**:
  - `reserve_account` — tries each non-disabled account in turn, inserting a
    `BotReservation` inside a `SAVEPOINT`; an `IntegrityError` (the EXCLUDE constraint
    firing) means "try the next account," not "fail." The constraint itself is the
    concurrency guard here — no row locking needed.
  - `reschedule_reservation` — **idempotent**: updates the existing reservation's
    `window` in place (same account) when that doesn't conflict; if it does, deletes it
    and reserves a different account. Calling it twice with the same `scheduled_at`
    touches nothing new.
  - `cancel_reservation` — deletes the reservation row and frees the account's live
    state (if a bot had already checked out), returning the affected account so the
    caller can remove it as a calendar guest.
  - `checkout_account`'s fallback path (no pre-existing reservation) now filters
    overlapping-reservation accounts **in SQL** via a correlated `NOT EXISTS`, combined
    with `LIMIT 1` — important detail: `FOR UPDATE SKIP LOCKED` *without* a `LIMIT`
    would lock every matching row in one statement (Postgres locks everything the
    `WHERE` clause matches when the `SELECT` executes, not lazily per fetched row),
    starving other concurrent callers of rows this call never intended to use. Caught
    this before it became a real concurrency bug.
- **`calendar_sync/guests.py` rewritten**: `sync_bot_guest` now reschedules the
  reservation first, then diffs the event's attendee list against old vs. new account
  email — removing the old one and adding the new one only if they actually differ
  (so a plain re-run adds nothing twice). `remove_bot_guest` (new) strips the bot's
  email from attendees on cancellation. `sync.py`'s cancel branch now calls
  `cancel_reservation` + `remove_bot_guest` instead of the old single-reservation
  release.
- **Client participant extraction**: `participants` gained an `email` column
  (migration `0003`). `calendar_sync/mapping.py:extract_client` finds the first
  attendee who isn't the organizer or a bot account, names them from the title's
  trailing `(...)` (falling back to that attendee's `displayName`), and `sync.py`
  stores them as a `Participant` with `role=client` on both create and update.
- **Live, read-only preview CLI**: `python -m calendar_sync.cli preview --employee
  it@fincelerate.com --limit 10` — lists upcoming matching events with their extracted
  Meet link, client, and languages. Writes nothing to any table (reads `bot_accounts`
  only, to exclude those emails from client matching). Run this first against the real
  domain before trusting the background poller.
- **`.env` / `.env.example` now hold your confirmed real config**: `CALENDAR_AUTH_MODE=
  service_account`, `EMPLOYEE_EMAILS=it@fincelerate.com`, `EVENT_TITLE_PREFIX=30 min
  with Information`, `AUTO_ADD_BOT_GUEST=true`, and the reservation window env vars
  (renamed per your message — see below). `fincelerate.notetaker1@gmail.com` and
  `fincelerate.notetaker2@gmail.com` are registered in the bot pool (`status=free`,
  placeholder profile paths under `./bot-profiles/`) — actual Chrome login/profile
  capture is still Phase 2. These are **free Gmail accounts outside fincelerate.com**,
  not Workspace users — see the follow-up update below for what that changes.
- **Renamed** `BOT_RESERVATION_TRAIL_SECONDS` → `BOT_RESERVATION_TAIL_SECONDS` to match
  your message exactly.

**New tests** (`tests/test_bot_pool.py`, `tests/test_calendar_guests.py`,
`tests/test_calendar_mapping.py`, `tests/test_calendar_sync.py` — 51 total, all
passing): two non-overlapping reservations landing on one account; overlap rejected at
the DB level (direct EXCLUDE-constraint test, not mocked); reschedule moves a
reservation in place; reschedule into a conflict swaps accounts (and the calendar
guest patch reflects the swap); reschedule run twice creates no duplicate; checkout's
fallback path skips an account with an overlapping reservation; client name from
title vs. `displayName` fallback vs. excluded bot/organizer attendees.

**Found while testing, not something you need to act on:** the test suite was
accidentally reading live values out of your real `.env` (`EVENT_TITLE_PREFIX`,
`AUTO_ADD_BOT_GUEST`) instead of being isolated, which made several sync tests fail
once you set real values. Fixed by having `test_calendar_sync.py`'s autouse fixture
force safe defaults (`EVENT_TITLE_PREFIX=""`, `AUTO_ADD_BOT_GUEST=false`) unless a test
explicitly opts in — worth knowing since it's a trap for the next calendar-related
`.env` change too.

### Update — bot accounts are external Gmail, not Workspace users

Corrected: `fincelerate.notetaker1@gmail.com` / `fincelerate.notetaker2@gmail.com`,
not `@fincelerate.com` addresses — they're outside the Workspace domain entirely, with
no relationship to `CALENDAR_AUTH_MODE`/domain-wide delegation (that's only for the
service account reading/editing **employee** calendars; bot identity is unrelated).

- **No code changes were needed.** `extract_client`'s exclusion already matches by
  exact email, not domain, so it was already correct regardless of where the bot
  accounts' addresses live — verified by re-reading `calendar_sync/mapping.py` before
  touching anything. Only data and docs were wrong: the two `bot_accounts` rows and the
  test fixtures that happened to use `@fincelerate.com` addresses for the bots. Both
  fixed (DB rows replaced, `tests/test_calendar_*.py` updated) — 51 tests still pass.
- **"Ask to join" / waiting-room / `bot_rejected` as the normal path**: this was already
  the designed join flow (see the original spec's Phase 2 bullets and the
  `bot_rejected` status already in `MeetingStatus`) — nothing to change in what's built
  so far, since the actual join logic is Phase 2. Flagging one real design question it
  raises for Phase 2, below.
- Being added as a calendar guest (`AUTO_ADD_BOT_GUEST=true`) does not guarantee Meet
  admits an external Gmail account automatically — that's a Meet "quick access" /
  host-admits-externally setting, independent of the calendar attendee list. The
  waiting-room flow is the real path regardless of the guest patch; the guest patch is
  just about calendar hygiene (so the invite shows who's joining), not admission.

### Open questions / risks for you

1. **`sync_bot_guest`'s calendar lookup still uses whichever `calendar_id` was passed
   into `sync_calendar`** (i.e. the owning employee's calendar) — unchanged from the
   first round's flag, restating since it's still relevant now that `AUTO_ADD_BOT_GUEST`
   is actually on.
2. **Still not run against your real Workspace domain** — I don't have the service
   account file in this environment. Once it's in place at
   `./secrets/service-account.json` (or wherever you point
   `GOOGLE_SERVICE_ACCOUNT_FILE`), run the preview CLI first:
   ```bash
   python -m calendar_sync.cli preview --employee it@fincelerate.com --limit 10
   ```
   It should find your Mon Oct 12 9:00–9:30 IST test event. If the client/Meet-link
   extraction looks right there, the background poller (same code path) will behave the
   same way.
3. **Domain-wide delegation scope**: since `AUTO_ADD_BOT_GUEST=true` is now set, the
   delegation grant needs `calendar.events`, not just `calendar.readonly` — worth
   double-checking in the Admin console if you set up delegation before this change.
4. **Reservations never get cleaned up** after a meeting completes (by design — a past
   reservation's window can't overlap a future one, so it's harmless) — but the table
   will grow unbounded. Not a problem at your current scale; flagging for whenever
   Phase 7's retention job gets built, since `bot_reservations` could use a cleanup
   pass too.
5. **For Phase 2**: `BOT_RESERVATION_LEAD_SECONDS=120` (2 min) controls when a
   reservation's window *opens*, not when the bot launcher actually starts the
   container — but now that bots are external Gmail accounts needing to sit in a
   waiting room before an admin/host admits them, 2 minutes may not be enough runway
   for "join, ask to join, wait to be admitted" before the meeting actually starts.
   Worth deciding the bot's actual launch-lead-time (distinct from the reservation
   window) when we design the scheduler in Phase 2 — not changing anything now since
   it's not this change request's scope.
