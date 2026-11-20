# LAN monitoring implementation plan

Branch: `feature/lan-monitoring`, based on `add-password-auth` at `c84e9fe`.

## Scope and identity

Track three connection roles separately: OBS/RTMP publishers, direct RTMP
viewers, and authenticated HLS/browser viewers. A connection is not a person:
IP addresses identify the endpoint observed by Nginx; a shared viewer account
cannot identify different people. Opening the dashboard is not viewing video.

## Capture and recovery

1. Private Nginx callbacks record RTMP publish/play start and end, keyed by
   application, stream, role and Nginx client ID. Duplicate callbacks are
   idempotent; reconnects after a disconnect create a new session.
2. Poll Nginx statistics every two seconds. Recover missed starts and end
   missing RTMP sessions only after six seconds of successful observations.
   A failed/malformed poll never disconnects everybody or fabricates zeros.
3. Existing HLS playlists/segments register an authenticated browser/IP/stream
   session. A lack of media requests for 30 seconds ends it at its last media
   request; this is an inferred end, not a TCP disconnect. Login revocation
   ends associated browser viewing sessions immediately. Raw auth tokens are
   never stored. HLS sessions count browser sessions, not HTTP requests.
   Browser observations are queued and drained in FIFO order outside media
   requests; the bounded queue reports dropped observations in monitor health.
4. Restart recovery closes stale database sessions at their last observation
   with an explicit estimated `monitor_restart` reason. No retroactive history
   is invented. Statistics recovery starts are labelled estimated as well.

Use one Nginx worker so its RTMP statistics cover all clients consistently,
and retain the existing single Python worker. Broadcasting/playback remains
enabled. Callback errors/timeouts fail open at Nginx so monitoring outages do
not become publishing authorization failures.

## Persistence

SQLite uses WAL, foreign keys, a busy timeout, short transactions and serialized
writes. Schema version 1 contains:

| Table | Purpose |
| --- | --- |
| sessions | Role, stream/application, client identity, LAN IP, viewer username, UTC start/last-seen/end, source and end reason |
| events | Append-only connect/disconnect events, event/observation time and estimation flag; unique per session/type |
| samples | Global RTMP bandwidth and connection counts sampled every 30 seconds |

Indexes cover open connection identity, time, stream/IP/role filters and event
pagination. Session/event history is retained; metric samples expire after 30
days. Database files live on a named Docker volume, outside the image/repo.
Transactions insert/update a session and its event together. Queries use bound
parameters. Reports clip durations to their requested interval and count peak
concurrency from session intervals, not browser memory or polling samples.

## API and UI

All monitoring reads use the existing viewer authentication. Internal callback
routes require the loopback proxy marker and are blocked on public port 8080;
public requests cannot supply that marker or spoof the forwarded client IP.

| Endpoint | Result |
| --- | --- |
| GET /api/monitoring/dashboard | Active counts/connections, current streams, recent events, monitor health, 24-hour sampled history |
| GET /api/monitoring/sessions | Paginated, filtered connection history |
| GET /api/monitoring/events | Paginated connect/disconnect log |
| GET /api/monitoring/reports | Summary, peak concurrency and breakdown by role, stream and IP |
| GET /api/monitoring/sessions.csv | Filtered session export, with spreadsheet-formula escaping |
| POST /internal/rtmp/{event} | Private Nginx-only notifications |

`m.html` has Live, Connection log and Reports views. They switch in-page so the
existing refresh-sign-in policy is preserved. Provide local-time display,
date/stream/IP/role filters, pagination, CSV export, responsive tables and real
loading/empty/error/stale states. No fake/demo data or external CDN dependency.
Bandwidth is explicitly RTMP bandwidth; RTMP statistics do not measure HLS
HTTP egress. IP fidelity requires the LAN deployment to preserve source IPs;
Docker Desktop/proxies may otherwise expose a gateway address.

## Validation and delivery

Test duplicates, reconnects, multiple devices sharing an IP, browser inactivity,
logout, recovery, failed snapshots, SQLite restart persistence, report boundary
clipping/peaks, filtering/pagination, auth/internal-route isolation, CSV escaping
and the existing player/session regressions. Validate Nginx configuration and
dashboard JavaScript. Do not start the streaming service as part of this work.
Publish the implementation on the new branch with deployment/backup guidance.
