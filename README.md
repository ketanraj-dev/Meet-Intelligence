# Meet Intelligence

Automatically joins Google Meet calls via a server-side bot, records them, diarizes and
transcribes them (multilingual, Indian languages + Hinglish), and saves the transcript
as a Google Doc. Sentiment analysis and an LLM-generated summary are planned but
currently paused — see "Plan (current)" below.

No Google Workspace / Meet APIs are used for **capture** — the bot joins and records as
a normal headless-Chrome participant, not via any Meet API. Docs + Drive APIs *are*
used, narrowly, for the transcript-export phase only (Phase 5) — a domain-wide-delegated
service account creates one Doc per meeting; see that phase's spec below.

This README grows one section per phase.

---

## Plan (current)

**Scope change:** sentiment analysis and the LLM-generated summary are paused — kept
documented here as a deferred future phase, not being built right now. The goal through
Phase 5 is: recording → diarization → transcription → transcript saved as a Google Doc.
Phases 1 and 2 (below) are unaffected and already done.

- **Phase 1 — Foundation** ✅ done. Bot account pool, Meeting/Participant schema, API,
  Google Calendar trigger.
- **Phase 2 — The meeting bot** ✅ done. Headless-Chrome join/record/leave, debounced
  speaker events, account rotation + weekly health check.
- **Phase 3 — Diarization + speaker naming** (next — kickoff message to follow
  separately). Turns `speakers.jsonl`'s participant join/leave + debounced
  speaking_start/stop events, plus the recorded audio, into labeled speaker segments.
- **Phase 4 — Transcription**. Indic-Transcribe (multilingual/Hinglish) with Whisper as
  fallback. Must run on CPU for local testing (slow is fine) — GPU support is a config
  switch added later, not a rewrite.
- **Phase 5 — Google Docs export only**. Originally spec'd bundled with sentiment +
  summary at the end of that phase; now its own phase, with the sentiment/summary
  content dropped from the Doc. Full spec below.
- **Deferred — Sentiment analysis + LLM summary**. Not scheduled. No phase number
  assigned yet — Phase 6 is already used below for the dashboard and Phase 7 for
  hardening/auth, so this will need to either slot in after Phase 5 (renumbering those
  two) or take whichever phase number is next free when it's picked back up.

### Phase 5 spec — Google Docs transcript export

- **Trigger**: once transcription (Phase 4) finishes for a meeting, create one Google
  Doc for it.
- **Auth**: Docs (`documents`) + Drive (`drive.file`) scopes via the same service
  account + domain-wide delegation already used for Calendar sync (see
  `calendar_sync/auth.py`'s `ServiceAccountDWDProvider`), impersonating
  `it@fincelerate.com`. Per the user, these scopes are already granted in the Workspace
  Admin console's domain-wide delegation grant alongside the existing Calendar ones.
- **Title**: `"Appointment – <Client> – <date, time IST>"`.
- **Content**: a header (host, client name/email, date/time IST, duration, languages
  detected) followed by the full transcript as `[hh:mm:ss] Speaker (role): text`, with
  overlapping speech marked. No summary or sentiment content in the Doc — that's the
  deferred phase above.
- **Folder**: `GOOGLE_DOC_FOLDER_ID` (already set in `.env` — a folder the user created
  by hand in `it@`'s Drive). **Open risk, test early**: with `drive.file` scope, the app
  may not be allowed to place files into a folder it didn't itself create. If that's
  the case live, fall back to the app creating its own "Meeting Transcripts" folder in
  `it@`'s Drive, store that folder's ID, and tell the user so they can share it with
  their team — do not request the full `drive` scope to work around this.
- **Sharing**: restricted, inherited from the folder. Never anyone-with-link.
- **Persistence**: `google_doc_id` and `google_doc_url` stored on `meetings`. Status
  flow: `transcribing → exporting → done`, with a separate `export_failed` status
  (retries) that never blocks or loses the already-computed transcript — Postgres
  (`transcript_segments`) stays the source of truth; the Doc is a generated,
  read-only export.
- **Idempotent**: re-running the export updates the existing Doc via its stored
  `google_doc_id`, never creates a duplicate.

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

---

## Phase 2 — The meeting bot

**Status: COMPLETE.** Verified end-to-end on a real two-person Google Meet call
(meeting `e140849a-6c26-4130-828d-b47a9990e7a1`): correct admission timing (no false
early "in_call"), the consent message posted to chat, both participants' real names
captured via join/leave events, speaking events logged for both participants, good
audio quality throughout, and a clean auto-leave at the end. See "Phase 2 close-out"
at the end of this section for the full verified-results writeup and the final round
of fixes that got it there. The rest of this section is the round-by-round build log
leading up to that, left as-is rather than rewritten.

**What's built:**
- `bot/` — the meeting bot container: `Dockerfile`, `entrypoint.sh`, `bot.py`,
  `selectors.yaml`, `requirements.txt`.
- `bot/bot.py` has two modes:
  - `BOT_MODE=login` — one-time interactive setup. Launches a **plain Chromium
    subprocess — not via Playwright** — against the persistent profile dir, plus a VNC
    server (port 5900) so you can log the bot's Gmail account in by hand; stays up
    until stopped, which terminates the process cleanly so the session is flushed to
    disk. No DB calls. See "Google blocks Playwright-driven sign-in" below for why this
    isn't just `launch_persistent_context`.
  - `BOT_MODE=run` (default) — the actual bot: `checkout_or_mark_unavailable` from the
    pool (still gates concurrency either way) → `status=joining` → launches Chromium
    (`BOT_JOIN_MODE=guest`, the default: a fresh temp profile, types the display name
    into Meet's "Your name" field — permitted since the Workspace Meet "Domain" setting
    allows non-signed-in users; `BOT_JOIN_MODE=signed_in`: the account's saved
    persistent profile instead) → `Ask to join` (the **normal** path either way, since
    even signed-in bot accounts are external Gmail, not `fincelerate.com` members) →
    waits for admission with a timeout → `status=in_call` on success, `bot_rejected` on
    timeout/denial → posts the consent chat message → polls participant count every
    250ms into `speakers.jsonl` → leaves when alone 60s, removed, max duration, **or a
    SIGTERM/SIGINT** → uploads whatever audio + speaker log exists → `status=recorded`
    → enqueues processing → releases the account immediately.
- **Audio**: ffmpeg records `meet_sink.monitor` (whatever Chrome plays to the PulseAudio
  null sink set as default) as 16kHz mono WAV, segmented every
  `audio_segment_seconds` (default 300s) so a crash never loses the whole call.
  **`audio_key` on `meetings` is a storage key *prefix*** (`meetings/<id>/audio/`), not
  a single object — there can be multiple segment files. Phase 3's diarization needs to
  list/concatenate under that prefix, not fetch one key.
- **Chrome choice**: Playwright's bundled **Chromium**, not Google Chrome — Chrome ships
  amd64-only (no arm64 build), Chromium has native arm64 builds, and Meet doesn't need
  anything Chrome-only for basic audio/video. Confirmed while building this: the image
  built and ran natively on this Apple Silicon host with no emulation warning (unlike
  `zenko/cloudserver` back in Phase 1).
- `bot_pool` wiring: `checkout_or_mark_unavailable` at start, a background thread
  calling `heartbeat` every `BOT_HEARTBEAT_INTERVAL_SECONDS`, `release_account` on every
  exit path (success, rejection, crash).
- `common/queue.py` — `enqueue_processing(meeting_id)`, a plain Redis list push.
  Phase 3 adds the real consumer; for now it just accumulates so nothing's lost.
- **Scheduler's bot-launch loop** (`scheduler/bot_launcher.py`, third thread in
  `scheduler/main.py`): every 15s, atomically flips `scheduled → joining` for meetings
  due within `BOT_LAUNCH_LEAD_SECONDS` (default 60s) and launches one bot container per
  meeting via the `docker` SDK. This is **docker-outside-of-docker**: the scheduler
  container launches *sibling* containers through the mounted host `docker.sock`, so
  bind-mount paths must be given in **host** terms — hence `HOST_PROJECT_DIR` (now set
  in `.env` to this repo's real absolute path on this machine).
- `login` setup mode (above) satisfies the "log the account in once, viewable via VNC"
  requirement from Phase 1's bot-account-setup doc.

**Deviations / judgment calls worth knowing about:**
- The bot does its **own** account checkout on startup (not the scheduler pre-selecting
  one and passing it in) — simpler, works identically whether launched by the scheduler
  or run by hand with just `MEETING_ID`, and `checkout_account`'s SKIP LOCKED semantics
  make this just as safe as a scheduler-side checkout would be.
- `docker.sock` is mounted into the `scheduler` container so it can launch bot
  containers — this gives that container full Docker API access (equivalent to root on
  the host). Fine at this stage; flagging as a real Phase 7 hardening item (e.g. a
  socket proxy restricting the API surface to just `containers.run`).
- The `bot-launcher` assumes a single-host Docker Compose deployment (it launches
  containers on *the* Docker daemon it can reach via the mounted socket). Moving to
  multiple hosts/Swarm/k8s later would need this rewritten — not a concern yet.
- `page.click(...)` calls throughout `bot.py` swallow failures and log a warning rather
  than crashing, **except** the two "ask to join"/"join now" buttons, where failing both
  raises — a pre-join toggle or a missing name field failing shouldn't abort the whole
  join; not finding any way to actually join should.

**Google blocks Playwright-driven sign-in ("This browser or app may not be secure")**:
hit this live on the first real login attempt. `launch_persistent_context` always
attaches the Chrome DevTools Protocol and sets `navigator.webdriver = true` — Google's
login flow actively fingerprints both and blocks sign-in, and there's no Playwright API
to suppress it (CDP attachment is the whole point of Playwright). Fix: `run_login_mode`
no longer touches Playwright's `launch()` at all. It reads `pw.chromium.executable_path`
(just a path lookup, doesn't launch anything) to find the Chromium binary Playwright
already downloaded, then launches *that same binary* directly via `subprocess.Popen`
with only `--user-data-dir`, `--no-sandbox` (needed since the container runs as root;
unrelated to automation detection), `--no-first-run`, `--no-default-browser-check`, and
a window size/position — no CDP, no `--enable-automation`, nothing Google's check keys
off. Verified the actual running process's command line via `/proc/<pid>/cmdline` has
none of `--enable-automation`/`--remote-debugging-port`/`--remote-debugging-pipe`
anywhere in the whole process tree. `BOT_MODE=run` is untouched and still uses
Playwright normally — Google's check blocks the interactive *sign-in* flow itself, not
an already-authenticated session being driven later, which is a meaningfully different
signal (session cookies vs. live CDP attachment during credential entry).

**Chromium tab crashes ("Aw, Snap!") in both modes** — also hit live, during the login
mode test above. Cause: Docker's default `/dev/shm` is 64MB; Chromium wants much more
shared memory for its tabs and crashes the renderer when it can't get it. Two-part fix,
applied to both modes:
- **The real fix**: `shm_size: "2gb"` on the `bot` service in `docker-compose.yml`, and
  `shm_size="2g"` passed to `client.containers.run(...)` in `scheduler/bot_launcher.py`
  (manually-run and scheduler-launched bots were two separate code paths that both
  needed it — compose's `shm_size:` doesn't apply to containers docker-py launches
  directly). Verified `df -h /dev/shm` inside a rebuilt container now shows 2.0G
  instead of 64M.
- **Fallback**: `--disable-dev-shm-usage` (Chromium falls back to `/tmp` for shared
  memory) on both the plain-subprocess login-mode launch and the Playwright
  `launch_persistent_context` args in run mode. Verified present on the actual running
  process via `/proc/<pid>/cmdline`.
- **Run mode now explicitly handles a page crash as a failure** rather than risking a
  silent half-dead state: `page.on("crash", ...)` logs a `page_crashed` error (with a
  hint pointing at `/dev/shm`) and sets a flag that `_wait_for_admission` and
  `_monitor_call` both check every loop iteration, raising `PageCrashed` — caught by its
  own `except` branch in `run_bot_mode`, setting `status=failed` with a clear error
  message. This matters because a crashed page's "Leave call" button also disappears,
  which the monitor loop would otherwise misread as a normal `removed_or_left` — the
  crash check runs first specifically to avoid that misclassification.

**Guest join mode (`BOT_JOIN_MODE=guest`, now the default)**: joins with a fresh
`tempfile.mkdtemp()` profile instead of the pool account's saved one — no Google
sign-in at all, just the "Your name" field on Meet's pre-join screen (already in
`selectors.yaml` as `pre_join.name_input`, reused as-is). The temp dir is cleaned up
(`shutil.rmtree`) in a `finally` that wraps every exit path, success or failure. The
bot pool is still checked out/released exactly as before — concurrency control doesn't
care which join mode is active, only the *browser profile* differs.
`AUTO_ADD_BOT_GUEST` is now gated on `bot_join_mode == "signed_in"` too (see
`calendar_sync/sync.py:_auto_add_bot_guest_enabled`), **on top of** its own env flag —
inviting a Gmail address as a calendar guest makes no sense when the bot never
actually signs in as that address. Your `.env` still has `AUTO_ADD_BOT_GUEST=true` from
before; it's simply inert now unless you switch to `signed_in`, since `guest` is the
new default — confirmed live (`_auto_add_bot_guest_enabled(get_settings())` → `False`
against your real `.env`).

**Reduced automation signals in run mode**: `launch_persistent_context` now passes
`ignore_default_args=["--enable-automation"]`, `args=["--disable-blink-features=
AutomationControlled"]`, and a normal desktop `user_agent` (`BOT_USER_AGENT`). Verified
on the live process's `/proc/<pid>/cmdline`: `--enable-automation` is absent and
`--disable-blink-features=AutomationControlled` is present. One thing this does
*not* remove, and can't: `--remote-debugging-pipe` (Playwright's actual CDP transport)
is still there — there's no way to drive a browser via Playwright without some CDP
channel, and that's fine, because it's a separate signal from what this was asked to
address. `--enable-automation` drives Chrome's own "being controlled by automated test
software" infobar and `navigator.webdriver`/`AutomationControlled`-gated JS behavior —
the things a page's own JavaScript can actually detect. The CDP pipe itself is a
driver-process IPC channel, invisible to page JS. This matters most for
`BOT_JOIN_MODE=signed_in`, where an already-authenticated Google account is what's at
stake if it gets flagged as bot-driven; applied to both join modes regardless since
it's harmless either way.

**SIGTERM/SIGINT now leave gracefully instead of getting SIGKILLed mid-call** — the gap
flagged at the end of the previous round. `run_bot_mode` registers handlers for both
signals that set a `shutdown_requested` flag; `_wait_for_admission` and `_monitor_call`
check it every loop iteration (same pattern as the crash flag), and `_monitor_call`
additionally attempts a best-effort click on "Leave call" before raising
`GracefulShutdown` — so the host sees a normal departure, not the bot just vanishing.
That exception is caught by its own branch that calls `_finalize_and_release` — the
*same* success-path helper used for a normal leave (stop ffmpeg, upload whatever
exists, `status=recorded`, release the account immediately) — not the failure path,
since a deliberate shutdown with a partial recording isn't a bot malfunction.
`docker-compose.yml`'s `bot` service got `stop_grace_period: 30s`, and
`scheduler/bot_launcher.py` passes the equivalent `stop_timeout=settings.
bot_stop_timeout_seconds` to `containers.run(...)` — both needed, same two-launch-path
reasoning as the `shm_size` fix.

**Found while wiring that env var through, not something you asked for**: the
scheduler's `containers.run(...)` call only ever passed a hand-picked handful of env
vars (`DATABASE_URL`, `REDIS_URL`, the `MINIO_*` ones, `LOG_LEVEL`) to auto-launched
bots — and those containers have no `.env` file at all (no bind-mounted repo, no
`env_file:`, unlike the manually-run `bot` service which gets the whole file via
`env_file: .env`). So `BOT_JOIN_MODE`, `BOT_DISPLAY_NAME`, every timeout, the new
`BOT_USER_AGENT` — all of it — would have silently fallen back to hardcoded Python
defaults for scheduler-launched bots while manually-run ones correctly picked up
`.env`, a real behavior split between the two launch paths that neither of us would've
noticed until a production bot behaved differently from local testing. Fixed by
replacing the hand-picked dict with `_bot_environment()`, which passes every `Settings`
field through as `FIELD_NAME: str(value)` — verified it produces 43 env vars including
`BOT_JOIN_MODE=guest`, where the old dict would have produced none of the new ones.

**What's verified vs. not — be clear-eyed about this:**
- **Verified live**, in this environment: the image builds and runs natively on arm64;
  `BOT_MODE=login` starts Xvfb/PulseAudio/dbus/x11vnc and a plain (non-Playwright)
  Chromium correctly (checked via `/proc/*/comm` and `/proc/*/cmdline` since the slim
  image has no `ps`) — confirmed zero automation flags anywhere in the process tree —
  and the VNC port actually accepts connections; stopping the container terminates
  Chromium cleanly and the profile directory persists real Chrome data to the host
  bind mount. Separately, `BOT_MODE=run` against a real meeting row with a **fake**
  Meet URL
  correctly checks out an account, starts ffmpeg (confirmed a valid 16kHz mono WAV was
  produced via `file`), fails cleanly with a clear error when no join button is found,
  sets `status=failed`, and releases the account back to `free` — the full mechanical
  pipeline (DB wiring, process management, error handling, cleanup) works. `/dev/shm`
  confirmed at 2.0G post-fix (was 64M), and `--disable-dev-shm-usage` confirmed present
  on the live process in both modes. **This round, also verified live**: guest mode's
  temp profile actually gets used (`--user-data-dir=/tmp/bot-guest-...` on the live
  process, not `/bot-profiles/...`); `--enable-automation` is absent and
  `--disable-blink-features=AutomationControlled` is present on the live run-mode
  process; `_bot_environment()` produces the expected 43-var env dict against the real
  `.env`, including `BOT_JOIN_MODE=guest`; `_auto_add_bot_guest_enabled()` correctly
  returns `False` against the real `.env` despite `AUTO_ADD_BOT_GUEST=true` there.
- **Tested via fakes, not live**: the `GracefulShutdown`/`PageCrashed` control flow
  inside `_wait_for_admission`/`_monitor_call` (11 unit tests against a fake
  Playwright-`Page`-like object — can't reach these code paths live without a real
  admitted call, same root cause as the DOM-interaction gap below). I did confirm live
  that signal registration/delivery itself works in this container (proven by
  `BOT_MODE=login`'s identical `SIGTERM` pattern responding correctly to `docker stop`)
  and that a SIGTERM arriving *before* either monitored loop (e.g. during the brief
  pre-join click sequence) doesn't hang — it just lets the bot's normal fast-fail path
  finish and exit on its own, confirmed by timing two runs (with and without an early
  `docker stop`) that landed within a couple seconds of each other (~24-27s, which
  turned out to be guest mode's genuine cold-start time for a brand-new profile
  directory, not a hang).
- ~~**NOT verified**: real Google Meet DOM interaction~~ — **since resolved.** This was
  true at the time (no Gmail credentials or a second human available in this
  environment to actually join a call with). You ran the real tests from here —
  several rounds of them, each selector fixed against the real captured DOM from the
  previous round's failure, not guessed. Every selector in `selectors.yaml` is now
  either confirmed-correct against real captures or explicitly flagged where it isn't
  (`speaking_indicator`, still CSS-verified but not yet instance-confirmed — see its
  comment there). See "Phase 2 close-out" at the end of this section.

### Setup

```bash
mkdir -p bot-profiles/notetaker1 bot-profiles/notetaker2 recordings  # already created
docker compose build bot
```

`HOST_PROJECT_DIR` in `.env` must be this repo's absolute path **on the host** (already
set). Without it, the scheduler's bot-launcher logs an error and skips launching —
manual `docker compose run bot` testing doesn't need it at all.

### Step-by-step: run a guest-mode test against a real Meet link (default, start here)

No profile setup needed — `BOT_JOIN_MODE=guest` is the default, so this is the fastest
path to a first real test.

1. Create (or open) a real Google Meet call — e.g. https://meet.google.com/new — and
   leave it open so you can admit the bot from the host side.
2. Create the meeting record (manual trigger, per your instruction — not via calendar
   sync):
   ```bash
   curl -X POST localhost:8000/meetings -H 'content-type: application/json' \
     -d '{"meet_url":"https://meet.google.com/<your-real-code>","scheduled_at":"2026-10-11T12:00:00Z"}'
   ```
   Copy the `id` from the response.
3. Run the bot directly against it (foreground, so you can watch it join live):
   ```bash
   docker compose run --rm -e MEETING_ID=<id-from-step-2> bot
   ```
4. In the real Meet call, watch for **"Fincelerate Notetaker (recording)"** asking to
   join — this is the normal path (your Workspace's Meet "Domain" setting allows
   non-signed-in users), admit it like any guest.
5. Check the Meet chat for the bot's consent message.
6. Talk for a bit, then either hang up, remove the bot, leave it alone for 60s+ to
   trigger the alone-timeout leave, or `docker stop <container-name>` (grab the name
   from `docker ps` — compose auto-generates one) to test the graceful-shutdown path:
   either way the bot should leave cleanly and upload whatever was recorded.
7. Watch the terminal: you should see `bot_starting` (`join_mode=guest`) →
   `admitted_to_call` → `consent_message_posted` → `leaving_meeting` → `bot_finished`.

### Step-by-step: save each bot's Chrome profile (only needed for `BOT_JOIN_MODE=signed_in`)

Skip this unless you're switching `BOT_JOIN_MODE` to `signed_in` — guest mode above
needs no profile at all.

1. Run login mode for the first account, publishing the VNC port:
   ```bash
   docker compose run --rm -d -p 5900:5900 \
     -e BOT_MODE=login -e BOT_PROFILE_PATH=/bot-profiles/notetaker1 \
     --name bot-login-notetaker1 bot
   ```
2. Connect any VNC client to `localhost:5900` (no password) — e.g. macOS Screen Sharing
   (`Finder > Go > Connect to Server > vnc://localhost:5900`), RealVNC, TigerVNC.
3. In the Chrome window, sign in to `fincelerate.notetaker1@gmail.com` (handle 2FA if
   prompted).
4. **Set the Google account's own display name to `Fincelerate Notetaker (recording)`**
   (Google Account settings, not a Meet setting) — Meet shows this automatically on
   join without the bot needing to edit anything in the join UI.
5. Stop the container — this closes the browser cleanly so the session persists:
   ```bash
   docker stop bot-login-notetaker1
   ```
6. Repeat for the second account (use a different host port, e.g. `-p 5901:5900`, if
   running both at once):
   ```bash
   docker compose run --rm -d -p 5900:5900 \
     -e BOT_MODE=login -e BOT_PROFILE_PATH=/bot-profiles/notetaker2 \
     --name bot-login-notetaker2 bot
   # ... sign in via VNC, set display name, then:
   docker stop bot-login-notetaker2
   ```
7. Run the same test as above, but with `BOT_JOIN_MODE=signed_in`:
   ```bash
   docker compose run --rm -e MEETING_ID=<id> -e BOT_JOIN_MODE=signed_in bot
   ```
   Expect `bot_starting` to log `join_mode=signed_in`, and the bot to show up in the
   Meet call as the signed-in `fincelerate.notetaker*@gmail.com` account instead of an
   anonymous guest.

### Step-by-step: check the recorded audio and speakers.jsonl

1. `curl localhost:8000/meetings/<id>` — confirm `"status": "recorded"` and that
   `audio_key` / `speaker_events_key` are populated.
2. Local copies land directly on the host (bind-mounted) even before upload:
   ```bash
   ls -la recordings/<id>/
   # audio_00000.wav  (audio_00001.wav, ... if the call ran past audio_segment_seconds)
   # speakers.jsonl       -- debounced participant join/leave + speaking events
   # speakers_raw.jsonl   -- same, but pre-debounce (every raw indicator flicker)
   ```
3. Play the audio (16kHz mono PCM WAV): `afplay recordings/<id>/audio_00000.wav` on
   macOS, or open with VLC/any player.
4. Inspect speaker events: `cat recordings/<id>/speakers.jsonl` — one JSON object per
   line. `t` is seconds since the recording started (same clock as the WAV),`ts` is
   wall-clock. Participant events:
   `{"t": 27.94, "ts": "...", "event": "participant_joined", "participant_id": "spaces/.../devices/437", "name": "Ketan Raj"}`
   (`participant_left` the same shape). Speaking events, debounced per
   `SPEAKING_MIN_BURST_SECONDS`/`SPEAKING_DEBOUNCE_GAP_SECONDS`:
   `{"t": 42.93, "ts": "...", "event": "speaking_start", "participant_id": "spaces/.../devices/437", "name": "Ketan Raj"}`
   (`speaking_stop` the same shape). The bot's own tile is never included.
5. The same two artifacts were also uploaded to object storage at
   `meetings/<id>/audio/` and `meetings/<id>/speakers.jsonl` in the `meet-intelligence`
   bucket — confirmed by `audio_key`/`speaker_events_key` being set on the meeting row
   in step 1 (the bot only sets those after a successful upload).

### Tests

`tests/test_bot_helpers.py` (selectors.yaml loads correctly, pure JSON/datetime
helpers, plus — new this round — `_wait_for_admission`/`_monitor_call` against a fake
Playwright-`Page` stand-in: admission success/timeout/denied/crashed, and
`_monitor_call`'s `removed_or_left`/`max_duration_reached`/crash/graceful-shutdown
paths including that the shutdown path actually clicks "Leave call") and
`tests/test_bot_launcher.py` (the atomic `scheduled → joining` claim never
double-claims, `due_meetings` respects the lead window and grace cutoff, launching
without `HOST_PROJECT_DIR` is a clean no-op) — 65 total, all passing. The fake-`Page`
tests are what actually exercise the crash/shutdown control flow end-to-end at the
logic level; nothing here spins up Playwright/a real browser — that's the "what's
verified vs. not" section above.

```bash
pytest
```

### Open questions / risks for you

1. **`selectors.yaml` needs live verification** against the real Meet UI — expect the
   first real test run above to fail at some selector and need an update. This is
   expected, not a sign something else is broken.
2. **`docker.sock` mounted into `scheduler`** — full Docker API access from that
   container. Flagging as a Phase 7 hardening item, not fixing now.
3. **`audio_key` is a prefix, not a single key** — make sure Phase 3's diarization step
   lists objects under it rather than assuming one file.
4. **The admission flow currently has no distinct "waiting, still pending" status** —
   a meeting sits at `joining` for the whole Ask-to-join wait, then flips to `in_call`
   or `bot_rejected`. If you want visibility into "currently in the waiting room" vs.
   "bot container hasn't started yet", that'd need a new status; didn't add one since
   you didn't ask for it and `joining` covers both reasonably.
5. **Chromium, not Chrome** — flagging again since it's a real behavioral difference
   from the originally-imagined "Chrome" bot, even though I think it's the right call
   for this stack (see "What's built" above).
6. ~~`run_bot_mode` doesn't register a `SIGTERM` handler~~ — **fixed this round**, see
   "SIGTERM/SIGINT now leave gracefully" above.
7. **Guest mode's cold-start is noticeably slower than signed_in's warm profile** — a
   brand-new temp profile directory takes longer to initialize than an
   already-used persistent one (~24-27s vs. the ~10-15s seen in the previous round,
   timed live). Not a bug, just a real cost of "fresh profile every time" — worth
   knowing if call-join latency ever matters (e.g. for `ADMISSION_TIMEOUT_SECONDS`
   budgeting), though it's still well inside the current 120s default.
8. **`BOT_JOIN_MODE` isn't validated** — anything other than exactly `"guest"` falls
   through to the `signed_in` branch (`account.profile_path`) rather than raising on a
   typo'd value. Low risk (two possible values, both meaningful), flagging rather than
   adding validation you didn't ask for.

### Phase 2 close-out — final verified real test + last round of fixes

Everything above this subsection is the round-by-round build log (left as written, not
retroactively edited) from first boot through several rounds of real-test-driven
selector/logic fixes. This is the final status.

**Final verified real test** (meeting `e140849a-6c26-4130-828d-b47a9990e7a1`, two real
participants, `BOT_JOIN_MODE=signed_in`):
- Admission timing correct — no false early `in_call` (the `participant_count >= 2`
  fix from an earlier round held up: the bot's own tile no longer gets mistaken for
  another participant in the waiting room).
- Consent message posted to the in-call chat successfully.
- Both participants' real display names captured, via `participant_joined` events
  keyed off each tile's `[aria-label^="Pin "]` descendant.
- `speaking_start`/`speaking_stop` events logged for **both** participants, confirming
  the `.DYfzY` speaker-circle CSS class selector (found in the real page's own
  compiled stylesheet — see `selectors.yaml`) actually works live, not just in theory.
- Audio quality good throughout (`audio_level_check` showed real speech, not silence).
- Clean auto-leave at the end.

**Final round of fixes, made directly from that test's real `speakers.jsonl`/debug
output:**
- **Speaking-event debounce.** The raw `speaking_indicator` reading flickers heavily —
  the real `speakers.jsonl` from the test above showed dozens of sub-second
  start/stop pairs from ordinary speech pauses. `_SpeakingDebouncer` in `bot.py` now
  merges a stop→start gap shorter than `SPEAKING_DEBOUNCE_GAP_SECONDS` (default 1.0s,
  same participant) into one continuous speaking period, and drops a burst shorter
  than `SPEAKING_MIN_BURST_SECONDS` (default 0.3s) entirely. Emitted events are
  backdated to the real raw transition time, not the (delayed) moment the debounce
  window confirmed them. The pre-debounce raw flicker is still logged, to a sibling
  `speakers_raw.jsonl`, in case the thresholds ever need re-tuning against real data.
- **`waiting_room` phase now actually gets logged.** It never did — `admission_phase_
  changed` went straight from `pending` to `in_call`, across every real test run so
  far, because `selectors.yaml`'s `waiting_text` (`"Asking to be let in"`) never once
  matched the real DOM (grepped three separate real captures for it and every
  plausible variant — zero matches each time). Since Ask-to-join → waiting room →
  admitted is the only real path for these external-Gmail bot accounts, "not yet
  admitted" is now treated as `waiting_room` directly, instead of depending on that
  unverified text to promote it out of `pending` — so a stuck wait is now visible in
  the logs regardless of whether that text selector ever matches.
- **End-of-call self-check for a silently-broken `speaking_indicator`.** If
  `audio_level_check` found real speech but the call produced zero confirmed
  `speaking_start` events, that's a strong signal the `.DYfzY` selector stopped
  matching (e.g. a future Meet UI change) — which would otherwise fail silently,
  producing empty-but-plausible-looking speaker data. `_monitor_call` now logs
  `speaking_indicator_selector_likely_broken` and returns a flag that `run_bot_mode`
  writes to the new `meetings.speaking_indicator_suspect` column (migration `0004`) —
  queryable per meeting instead of only discoverable by reading logs after the fact.
- **Bot build version logged on startup.** A prior round's test accidentally ran
  against a stale image (forgot to rebuild after a code change) and that was only
  discovered after the fact. There's no `.git` directory in this checkout to pull a
  commit hash from, so `bot/Dockerfile` bakes a UTC build timestamp into the image
  instead (`bot/BUILD_VERSION`, written by a `RUN date ...` layer that only reruns —
  producing a fresh timestamp — when the source actually changed). `main()` logs it as
  `bot_code_version` before doing anything else, so the running build is visible
  immediately instead of only inferable from behavior.

**Known gap going into Phase 3**: `speaking_indicator` is CSS-verified (pulled from
the real page's own compiled stylesheet, not guessed) but still hasn't been seen
actively matching mid-utterance in a captured screenshot — every debug capture so far
happened to land on a silent instant. The live test above confirms it works in
practice (both participants produced real speaking events), so this is now a
theoretical/documentation gap, not a functional one.

---

## Bot pool: account rotation + weekly health check

Two problems with a small, fixed pool of Gmail accounts: (1) always picking the
same account first (e.g. lowest `id`) leaves the others idle, and an idle Google
session can silently expire; (2) nothing ever checked whether a signed-in session
was still actually valid until a real meeting tried to use it and failed to join.

**Rotation**: `bot_accounts.last_used_at`, set only at checkout (not reservation,
not release). Both `reserve_account` and `checkout_account`'s fallback path order
candidates by `last_used_at ASC NULLS FIRST` instead of a fixed `id` order — a
never-used account always goes first, otherwise whichever has sat idle longest.

**Weekly health check**: a 4th scheduler thread (`scheduler/health_check.py`) polls
hourly for any currently-*idle* (`status=free`) account whose `last_health_check_at`
is unset or older than `BOT_HEALTH_CHECK_INTERVAL_SECONDS` (default 7 days), and
launches a one-shot `BOT_MODE=health_check` bot container per due account — **never**
against an `in_use` account, since a second Chromium against the same profile
directory would collide with the real bot's own instance on Chrome's `SingletonLock`.

That container (`run_health_check_mode` in `bot/bot.py`) opens the account's saved
profile with real Playwright (no credential entry happens, so none of
`run_login_mode`'s automation-detection concerns apply), navigates to
`myaccount.google.com`, and checks whether it got redirected to Google's own sign-in
page. The actual check runs in a background thread joined against a hard
`BOT_HEALTH_CHECK_RUN_TIMEOUT_SECONDS` ceiling (default 90s) — confirmed live that a
stale profile lock can otherwise hang this far longer with nothing to show for it; a
stuck run is simply abandoned (daemon thread) rather than waited out. Each step
(`launching_chromium` / `navigating` / `checking_signed_in`) is logged individually,
and a screenshot is saved locally (under `<recordings_dir>/health_checks/<account_id>/`)
whenever the check itself fails partway through — a useful artifact for a human
investigating why one account keeps coming back inconclusive. The result lands on
`bot_accounts.last_health_check_at` / `needs_relogin` (and is exposed via
`GET /bot-accounts`) — `needs_relogin=true` is the alert signal, same convention as
`NO_BOT_AVAILABLE`/`bot_launch_failed_permanently` elsewhere in this project (no
outbound webhook channel exists yet). A failed or timed-out check is treated as
*inconclusive*, not proof the session is broken — it keeps whatever `needs_relogin`
value was already there rather than guessing from a transient failure.

**Manual run** (to check one account by hand, outside the weekly schedule):

```bash
docker compose run --rm -e BOT_MODE=health_check -e BOT_ACCOUNT_EMAIL=notetaker1@gmail.com bot
# or, if you already have the UUID:
docker compose run --rm -e BOT_MODE=health_check -e BOT_ACCOUNT_ID=<uuid> bot
```

`python -m bot_pool.cli list` now also prints `last_used`, `last_health_check`, and
`needs_relogin` for each account, so a quick glance shows both rotation health and
whether anything needs a manual re-login (`docker compose run --rm -d -p 5900:5900 -e
BOT_MODE=login -e BOT_PROFILE_PATH=<profile> bot`, same as initial setup).

**Stale profile locks**: confirmed live — an old `login`-mode container that didn't
exit cleanly left a `SingletonLock` in notetaker1's profile dir ("profile in use by
another Chromium process on another computer `<hostname>`"), which made the *next*
launch against that profile (a health check) hang for the full old timeout instead of
just failing fast. Chromium only removes `SingletonLock`/`SingletonCookie`/
`SingletonSocket` on a clean exit — a SIGKILL, an OOM, or a crashed container all skip
that cleanup, and a container that didn't get `stop_grace_period`'d cleanly leaves the
lock behind for the next launch to trip over. Fixed two ways:
- **Before launching Chromium in any mode** (`run`, `login`, `health_check`), any
  existing lock file is cleared first — logged as `stale_profile_lock_cleared` — as
  long as it's actually safe: the bot pool's own `in_use` status is the source of
  truth for who's legitimately allowed to hold a profile right now. `run` mode's
  checkout already proved exclusive ownership (row-locked), so it clears
  unconditionally; `login`/`health_check` modes re-check the account's current status
  and leave a genuinely `in_use` account's lock alone.
- **`login` mode now force-clears the lock after shutdown**, not just relies on
  Chromium's own cleanup — if the 10s graceful-SIGTERM window expires and it has to
  SIGKILL, Chromium never gets the chance to clean up after itself; this guarantees
  the profile is left clean for the next launch either way.

**Postgres healthcheck fix**: `docker-compose.yml`'s `postgres` healthcheck was
running `pg_isready -U ${POSTGRES_USER:-meet}` with no `-d` — confirmed via `psql -l`
that no database literally named `meet` exists (only `meet_intelligence`), so it
wasn't actually checking connectivity to the database every other service's
`DATABASE_URL` depends on. Added `-d ${POSTGRES_DB:-meet_intelligence}`.

---

## Phase 3 — Diarization + speaker naming

**What's built:** a new `worker/` service (own `Dockerfile`/`requirements.txt`,
mirroring `bot/`'s pattern) that consumes the Redis queue `enqueue_processing`
(Phase 2) pushes to. For each `recorded` meeting: downloads and concatenates all its
audio segments (plain stdlib `wave`, no ffmpeg needed), runs
`pyannote/speaker-diarization-3.1` on CPU, maps pyannote's anonymous clusters
(`SPEAKER_00`, ...) to real participant names by total time-overlap against
`speakers.jsonl`'s debounced `speaking_start`/`speaking_stop` intervals (same `t`
clock as the WAV), resolves overlapping speech to a single dominant speaker per
region, merges/splits into Phase-4-ready turns (same-speaker gaps < 0.5s merged,
turns > 30s split at the quietest ≥250ms silence point found, else a hard cut), and
writes them to a new `speaker_turns` table — idempotent (replaces, never
duplicates). Status: `recorded → diarizing → diarized` (or `failed`, with up to 3
retries — `worker/main.py`'s `_handle_meeting`, same convention as
`bot_launcher.py`'s launch-retry logic from an earlier round).

`worker/cli.py` gives `process-meeting <id>` (run/re-run one meeting directly,
bypassing the queue) and `show-turns <id>` (print turns with names/times/confidence).

**Real dependency/runtime bugs found and fixed getting this to actually run** (every
one confirmed live, not guessed — this dependency stack has almost no margin for
unpinned versions):
- **`torch`/`torchaudio` version mismatch**: unpinned install resolved torch 2.14.1 +
  torchaudio 2.11.0 (not even matching minor versions) — pyannote.audio 3.1.1 calls
  `torchaudio.set_audio_backend(...)`, an API torchaudio removed well before 2.11.
  Pinned both to `2.1.1`, the combo pyannote.audio 3.1.x was actually built against.
- **NumPy 2.x ABI mismatch**: that same torch 2.1.1 build was compiled against the
  NumPy 1.x ABI; letting pip resolve latest NumPy (2.x) broke torchaudio's compiled
  extension at import. Pinned `numpy<2`.
- **Missing `libgomp1`**: torchaudio's compiled extension needs the GNU OpenMP
  runtime; `python:3.11-slim` doesn't ship it. Added to the Dockerfile's apt packages
  (alongside `libsndfile1` for `soundfile`, pyannote's audio-loading backend).
- **`huggingface_hub` removed `use_auth_token`**: pyannote.audio 3.1.1's
  `Pipeline.from_pretrained(..., use_auth_token=...)` forwards that exact kwarg name
  to `hf_hub_download()`, which a current `huggingface_hub` no longer accepts
  (`token=` now). Pinned `huggingface_hub==0.25.2`, from pyannote.audio 3.1's own era.
- **OOM on a single ~2-minute meeting**: pyannote's own default `segmentation_batch_size`/
  `embedding_batch_size` (32) spiked the worker container from ~0.8GB to 5GB+
  resident memory within two seconds and got SIGKILLed (confirmed live, repeatedly,
  with `docker stats` sampled every second during the spike). `diarization_batch_size`
  (new setting, default `1`) fixed it outright — slower per job, not faster, but it
  actually completes instead of dying; appropriate for "CPU, slow is fine" per the
  current testing setup. Also added `torch.set_num_threads(1)` and
  `OMP_NUM_THREADS=1`/`MKL_NUM_THREADS=1` (the BLAS thread pools torch's own setting
  doesn't fully control) as belt-and-suspenders against the same class of spike.
- Added a persistent `worker_model_cache` volume (`/root/.cache`) — without it, every
  `docker compose run --rm worker ...` CLI invocation (a fresh, disposable container)
  re-downloads the model from HF from scratch.

**Validated live** on two real recorded meetings:
- `0185a061-3954-48e6-a462-c1073389d1e9` (129s audio): 9 turns, pyannote took 294s
  (~2.3x realtime, CPU). Only one participant ("Ketan Raj") ever had logged
  `speaking_start`/`speaking_stop` events in this meeting's `speakers.jsonl` — the
  other participant simply has none on record — so every pyannote cluster correctly
  had only one real candidate to map against; confidence scores (0.19–0.76, never
  forced to 1.0) honestly reflect that pyannote found more speech than the two known
  intervals cover, rather than claiming false certainty.
- `e140849a-6c26-4130-828d-b47a9990e7a1` (229s audio): 13 turns, pyannote took 519.7s
  (~2.3x realtime). `speakers.jsonl` has real logged speaking data for *both*
  participants here (44 events for "Information Technology", 50 for "Ketan Raj"), but
  pyannote's own clustering found essentially one dominant voice cluster
  (`SPEAKER_00`, correctly mapped to "Information Technology") plus a handful of
  very short stray `SPEAKER_01` blips that didn't overlap enough with anyone to map
  confidently (`Unknown speaker 1`) — confirmed by inspecting pyannote's raw output
  directly, independent of this project's own mapping code. Per the user: in most of
  this test recording, one person was speaking from *both* Meet devices/tiles, so a
  single dominant acoustic voice is the correct real-world answer here, not a
  diarization failure — this meeting isn't a clean two-distinct-voices accuracy test.
  **Not yet validated**: name-mapping accuracy on a recording with two genuinely
  distinct human voices overlapping/alternating throughout, which neither available
  meeting actually provides.

**Known limitation to flag**: the bot records `meet_sink.monitor` — the single
mixed-down stereo-to-mono output of everyone's audio as the bot hears it, not
per-participant tracks. pyannote's acoustic clustering accuracy on a *real* multi-voice
call therefore depends entirely on how distinguishable the voices are in that one mixed
stream — something only a genuine multi-voice test recording can actually validate.

**Open items for you / future rounds:**
1. CPU diarization runs at ~2.3x realtime here — a real multi-minute meeting will take
   multiple times its own length to diarize. `DIARIZATION_DEVICE=cuda` is wired as a
   config switch (not a rewrite) for whenever a GPU host is available, per the
   original ask — just not exercised live yet (no GPU in this environment).
2. `worker`'s retry backoff (`RETRY_BACKOFF_SECONDS = 10`) is a plain `time.sleep` in
   the failure handler, not a separate scheduling thread — fine at today's volume, may
   want revisiting if the queue ever gets busy enough for that to matter.
3. `speaker_turns.participant_id` deliberately is NOT a foreign key to `participants`
   (calendar-invite identities) — it's the raw Meet-session participant id from
   `speakers.jsonl`. See the model's own docstring in `common/models.py` for why.

## Phase 4 — Transcription

**What's built:** a new `asr/` service (own `Dockerfile`/`requirements.txt`,
deliberately a separate container/process/queue from `worker/` — pyannote and the ASR
backends must never be loaded in the same process at once, see
`asr/transcription.py`'s module docstring) that consumes `enqueue_transcription`
(pushed by `worker/main.py` right after a meeting's diarization succeeds). For each
`diarized` meeting: decides a language **per speaker** (not per clip — majority vote
of faster-whisper's language ID across that speaker's 3 longest turns, normalizing
Hindi-confusable results `ur`/`mai`/`bho` to `hi`; `expected_languages` on the meeting
overrides LID entirely), transcribes every turn with `IndicTranscribeASR`
(`bodhan-ai/indic-transcribe-flex`) when the decided language is one of its 27, else
`WhisperASR` (faster-whisper, CPU, `small` model for testing), and writes
`transcript_segments` — idempotent (delete-then-insert, replaces, never duplicates).
Status: `diarized → transcribing → transcribed` (or `failed`, up to 3 retries, same
convention as `worker/main.py`'s `_handle_meeting`).

`asr/cli.py` gives `transcribe-meeting <id>` (`--limit N` to transcribe only the first
N turns for a quick check without replacing the full transcript — logs a warning that
it's a partial replace) and `show-transcript <id>` (prints
`[hh:mm:ss] Speaker (role): text`, with an `[overlap]` marker carried over from
diarization).

**Credit (per the Indic Open Model License):**

> Built with Indic-Transcribe from Bodhan AI / AI4Bharat.

Self-hosted for this project's own internal use only — not exposed as an API for
third parties, per the license's "ask before hosting it for others" condition.

**Real bugs found and fixed getting this to actually run:**
- **Wrong integration pattern entirely**: the original implementation called
  `transformers.pipeline("automatic-speech-recognition", model=..., trust_remote_code=True)`,
  built on the standard Whisper-family HF pattern since there was no internet access
  to confirm the real API at implementation time. On first real run this failed
  (`ValueError: ... contains custom code which must be executed...`, then even with
  `trust_remote_code=True`, `Couldn't instantiate the backend tokenizer... need
  sentencepiece or tiktoken`). Fetching the model repo's own `README.md`/`inference.py`/
  `indic_transcribe.py` directly from the Hub revealed it isn't a
  `pipeline()`-compatible model at all — it ships its own `IndicTranscribe` class with
  a custom dual-SentencePiece tokenizer that loads files via `os.path.join` and
  therefore needs a real local directory, not a Hub repo id or `AutoTokenizer`'s
  dynamic-class resolution. Fixed by `snapshot_download`-ing the whole repo, putting
  it on `sys.path`, and importing the repo's own `IndicTranscribe.from_pretrained(...)`
  directly (`asr/transcription.py`'s `IndicTranscribeASR._pipeline()`). This also
  surfaced the model's *real* 27-language list and `MODES` dict straight from its
  source — replacing the task spec's best-effort guess in `common/config.py`.
- **Missing `sentencepiece`**: the custom tokenizer needs it to load two `.model`
  files directly (not a `tokenizers`-library fast-tokenizer file). Added to
  `asr/requirements.txt`.
- **Missing `torchaudio`**: the model's own audio-loading code resamples with it when
  a clip's sample rate doesn't match the model's expected rate. Added.
- **ASR container pinned to 1 CPU thread, using only 1 of 10 available Docker Desktop
  cores**: `OMP_NUM_THREADS=1`/`MKL_NUM_THREADS=1` and `torch.set_num_threads(1)` were
  copied over from `worker/Dockerfile`'s pyannote-specific OOM fix (Phase 3) without
  rethinking them for this container's own memory headroom — confirmed live via
  `docker stats` (99% CPU on one core, 6.3/10.7GiB memory sitting unused). Fixed with a
  new `ASR_NUM_THREADS` setting (default 8, leaves 2 cores for macOS + other
  containers) threaded into both `torch.set_num_threads()` (`IndicTranscribeASR`) and
  faster-whisper's `cpu_threads=` (`WhisperASR`), plus the matching
  `OMP_NUM_THREADS`/`MKL_NUM_THREADS` env vars moved from the Dockerfile into
  `docker-compose.yml`'s `asr` service so they're configurable without a rebuild.
  `worker`'s diarization thread pinning is untouched — that OOM fix was for a
  different model/container and still applies there.

**Validated live** on two real diarized meetings (8 threads, CPU):
- `4b08e233-fd3c-4277-b243-ab93b3f5f9cd` (Hindi/English mixed, 12 turns): full
  transcription took 71.6s (~6s/turn average) after the thread fix, down from 516s
  single-threaded (~24s/turn) before it. `docker stats` showed 700-770% CPU (7-7.7 of
  the configured 8 cores) and ~1GB memory peak — nowhere near the 10.7GB available.
- `e140849a-6c26-4130-828d-b47a9990e7a1` (English, 7 turns): 64.6s (~9s/turn average).

**Known accuracy issue — per-speaker language decision on code-switching speakers:**
on `4b08e233`, both speakers were decided as `used=en` (majority vote of faster-whisper
LID across each speaker's 3 longest turns). For "Information Technology" that's
correct — their speech is genuinely English throughout. For "Ketan Raj" it's wrong for
part of their turns: he code-switches between Hindi and English mid-conversation, and
whichever language happened to dominate his *longest* sampled turns won the vote for
*all* of his turns. The Hindi turns, transcribed under English mode, come out as
garbled near-nonsense rather than correct Devanagari+Latin mixed-script output, e.g.:
  - `"Test karne ke liye kuch to go lage haan Teera awaaz nahi aa raha hai"`
  - `"Are you matlab voice zero per Above allah"`
  - `"But sir voice direct directoration use kare taki different voices test kar sake wo"`
  This is the per-speaker (not per-clip) design working exactly as specified — the
  spec explicitly chose one decision per speaker over one per turn — but it has a real
  failure mode on a speaker who code-switches, which neither of this session's two test
  meetings' *other* speaker exercises (both "Information Technology"/this meeting's
  second speaker are single-language throughout). Worth you deciding whether to accept
  this tradeoff, move the language decision to per-turn (slower — doubles LID calls),
  or treat code-switching as a flag to route to Whisper instead. Two turns on this same
  meeting (`00:00:58` and `00:01:19`) also show severe ASR repetition-loop
  hallucination (`"Hello"` and `"ha ha"` repeated 50-150+ times) — a known failure mode
  of this model family on low-signal/edge-of-speech audio (see its own README: "a
  single decoding pass collapses into repetition" past its training distribution);
  worth checking whether those specific turns are mostly silence/noise.
- One line of `4b08e233`'s transcript (Ketan Raj, `00:01:15.790`) is redacted
  (`REDACTED`) in this chat's own display — Claude's output filter, not something
  stored that way in the database — check that segment directly via `show-transcript`
  in your own terminal if you want to see it.
- `e140849a`'s transcript is clean throughout (all genuinely English, coherent, no
  repetition or language-mismatch issues) — confirms the pipeline mechanics are sound;
  the issue above is specifically about the per-speaker LID design, not a bug in the
  transcription/storage/CLI plumbing.

**Open items for you / future rounds:**
1. The per-speaker code-switching accuracy issue above.
2. `IndicTranscribeASR`'s wrapper (per its own docstring) decodes whole clips and
   degrades past ~30s (training `max_duration`); this project's turns are already
   capped at `diarization_max_turn_seconds=30.0` from Phase 3, so the repo's own
   `long_form`/chunking path was never needed here — but worth keeping in mind if that
   cap ever changes.
3. `TRANSCRIPTION_DEVICE=cuda` is wired as a config switch (not a rewrite) for
   whenever a GPU host is available — not exercised live yet (no GPU in this
   environment).
