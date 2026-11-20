# rtmp-streaming
Streaming to a local network from OBS using NGINX-RTMP, with a password for
browser viewers:

- **Streaming (OBS):** use the existing stream name as the stream key (for example `test`). No publishing password is required.
- **Watching (browser):** the player pages, HLS video, dashboard, API and
  WebSocket on port `8080` all ask for a username and password.
- **Watching (direct RTMP):** playback on port `1935` remains enabled without authentication.

Passwords are **not** stored in the image. The container reads them from
environment variables when it starts, so the same image works on any server.

## Setup

1. Create your password file from the template and fill it in:
   ```
   cp stream.env.example stream.env
   ```
   ```
   VIEWER_USER=viewer
   VIEWER_PASSWORD=<viewer password>
   ```
   `stream.env` is git-ignored. Keep it private.

2. Start it:
   ```
   docker compose up --build -d
   ```
   If `stream.env` is missing or incomplete, the container stops with a
   message saying what's missing. Browser access always requires the viewer login.

3. In OBS, use `rtmp://<host-ip>:1935/live` as the server and
   your existing stream name as the stream key (for example `test`, without a
   `?pwd=...` suffix).

4. Open `http://<host-ip>:8080/` (or `/m.html`, `/player.html`) and log in.

## Viewer login and refresh

The viewer login uses a sign-in form and an HttpOnly session cookie. Existing
`VIEWER_USER`, `VIEWER_PASSWORD`, `VIEWER_PASSWORD_HASH` and mounted `.htpasswd`
files continue to work. SHA-512 crypt, SHA-256 crypt and Apache apr1 hashes are
supported.

- Each successful sign-in admits one viewer page load.
- Refreshing the browser, opening another viewer page or restoring a page with
  Back/Forward requires signing in again. The old session is revoked on the
  server; replaying its cookie cannot restore access.
- HLS playlists/segments, API calls and live dashboard updates use that session
  without extra login prompts. In-page controls such as **Refresh Data** do not
  sign you out.
- A session also expires after 12 hours or when the container restarts.
- Tabs in the same browser share the session cookie: refreshing a viewer page
  signs that browser session out, including its other open viewer tabs.

Session state is held in the single Python process started by `start.sh`. Keep
one API worker per container; multiple workers/replicas would need a shared
session store.

After updating the source, rebuild once with `docker compose up --build -d`.

To change a password, edit `stream.env` and run `docker compose up -d` again.
No rebuild needed.

Stop the service with:

```
docker compose down
```

## Variables

| Variable | Required | Meaning |
|---|---|---|
| `VIEWER_PASSWORD` | yes* | Browser password. Hashed when the container starts. |
| `VIEWER_PASSWORD_HASH` | yes* | Use instead of `VIEWER_PASSWORD` to avoid keeping the plain password on the server (create with `openssl passwd -6`). |
| `VIEWER_USER` | no | Browser username, default `viewer`. |

\* Set one of the two.

Alternative to env vars: run `./make-secrets.sh viewer` to create a `secrets/`
folder and mount it at `/etc/nginx/secrets` instead.

## Ports

- `1935`: RTMP publishing and direct playback, without authentication
- `8080`: web pages, HLS, dashboard, `/api` and `/ws`, all behind the login

The API server listens on `127.0.0.1:8000` inside the container only. The
dashboard reaches it through nginx on `8080`, so it is protected by the same
login.

Nginx serves media files through internal locations after the Python session
check. Stream monitoring uses a separate loopback-only statistics listener on
port `8090`; that port is not published by Docker Compose.

Direct playback at `rtmp://<host-ip>:1935/live/<name>` does not use the browser
login. Anyone who can reach port `1935` can publish or watch via RTMP.

> Plain HTTP sends viewer passwords unencrypted. Use this setup on a trusted
> network. Add HTTPS for browser access and restrict RTMP access before exposing
> the service to the internet.

## LAN monitoring, history and reports

The `feature/lan-monitoring` branch builds on the viewer login and stable player
dropdown. The implementation plan is in [docs/monitoring-plan.md](docs/monitoring-plan.md).

Open `http://<host-ip>:8080/m.html` and sign in with the existing viewer account.
The dashboard has three views:

- **Live overview:** broadcasters, browser/direct-RTMP viewers, distinct LAN IPs,
  current streams/quality, RTMP bandwidth, recent events and sampled traffic history.
- **Connection log:** persistent sessions or individual connect/disconnect events,
  filtered by local dates, exact stream name, LAN IP and role. Session history also
  supports active/ended filtering and pagination.
- **Reports:** sessions overlapping the selected dates, distinct IPs, combined
  connection time, peak concurrency, and breakdowns by role, stream and IP. The
  stream/IP breakdowns show the top 100; CSV export includes all matching sessions.

These views switch in-page. Refreshing the entire browser still requires signing
in again. Dashboard access is protected by the same viewer login; it does not
itself count as watching video. There are no separate administrator accounts.

### What is recorded

Each session records role, application/stream, Nginx-observed LAN IP, viewer
username where applicable, UTC start/last-seen/end times and observation source.
The browser displays times in its local timezone. CSV timestamps are UTC ISO 8601.
Multiple sessions may share one IP; IP counts identify endpoints, not people.
No raw viewer session tokens are stored in SQLite.

RTMP publish/play callbacks record connection boundaries. Polling every two
seconds recovers missed callbacks and confirms missing clients over a six-second
grace period. Failed/malformed polls retain last-known state and mark it stale.
Only a publisher makes a stream active; viewers waiting for a publisher do not.

HLS is a sequence of HTTP requests, so it has no reliable TCP-style disconnect
event. Existing media requests establish/touch a browser session; missing files
do not count. Thirty seconds without media activity ends the session at its last
persisted observation. Touches are throttled to five seconds. Refresh/logout
revokes associated viewing sessions through a bounded queue drained within the
monitoring cycle. Media observations use that queue too, so database writes do not
hold up media responses. Inferred durations show **≈**, and events include the reason/estimation flag.
Pause, buffering and abrupt network loss can affect inferred browser durations.

Monitor restarts close old database sessions at their last observation with the
`monitor_restart` reason. History begins when monitoring is deployed. Connections
that begin and end entirely during a monitoring outage cannot be reconstructed.
Nginx callbacks fail open if monitoring is unavailable; they do not add RTMP
authentication or disable direct playback.

Bandwidth measurements are **RTMP-only** and exclude HLS HTTP egress. Metrics are
sampled every 30 seconds and displayed in five-minute buckets; they are not used
to calculate report peak concurrency. Session/event history is retained, while
metric samples are kept for 30 days.

### LAN IP fidelity and deployment

Nginx overwrites forwarded-IP headers with the actual client address, and callback
routes are reachable only through its loopback listener. Do not publish port 8000
or 8090. SQLite and RTMP statistics use a single Python worker and a single Nginx
worker; this is intentional for the LAN deployment.

The network must preserve source IPs. Docker Desktop, NAT or another proxy may
make devices appear under a gateway IP; application code cannot recover an
original TCP source address that the network has discarded. Verify two real LAN
devices appear with distinct expected addresses on the target host before relying
on per-IP reports. Do not trust arbitrary client-supplied forwarded-IP headers.

On the deployment host, check out `feature/lan-monitoring`, keep `stream.env`, then:

```sh
docker compose build
docker compose run --rm --no-deps --entrypoint nginx streaming -t
docker compose up -d
```

SQLite lives at `/var/lib/stream-monitor/monitoring.sqlite3` on the named
`monitoring_data` Docker volume. Container rebuilds/restarts and `docker compose
down` retain that volume. `docker compose down -v` deletes it and its history.
Keep the volume on a local disk, not a network filesystem.

Use SQLite's online backup API rather than copying a live database file without
its WAL. For example:

```sh
docker compose exec -T streaming python3 - <<'PY'
import sqlite3
source = sqlite3.connect('/var/lib/stream-monitor/monitoring.sqlite3')
target = sqlite3.connect('/var/lib/stream-monitor/monitoring-backup.sqlite3')
source.backup(target)
target.close()
source.close()
PY
docker cp streaming:/var/lib/stream-monitor/monitoring-backup.sqlite3 ./monitoring-backup.sqlite3
```

### API and checks

`/api/monitoring/dashboard`, `/sessions`, `/events`, `/reports` and `/sessions.csv`
are under the `/api/monitoring` prefix and require viewer authentication. History
and report filters are `start`/`end` (ISO 8601 with UTC offset), `stream`, `ip` and
`role` (`publisher`, `rtmp_viewer`, `hls_viewer`). Sessions additionally accept
`status=active|ended`. Paginated endpoints accept `limit` (1–200) and `offset`.
Date intervals are half-open; the UI converts its inclusive local end date.
Report connection time is clipped to the interval; CSV exports full session
durations and an estimation flag. CSV exports escape spreadsheet formulas.

Install the application dependencies plus `httpx`, then run these checks without
starting Nginx, OBS or a streaming listener:

```sh
python3 -m unittest discover -s tests -v
node --test tests/test_player_dropdown.test.cjs tests/test_monitor_ui.test.cjs
```

An optional DOM smoke test exercises all three views, API response rendering,
filters, CSV actions, empty/error/recovery states and safe rendering of stream
names. It generates fixtures through a temporary SQLite database and starts no
service. Install its test-only dependency outside the repository:

```sh
npm --cache /tmp/stream-monitor-npm-cache install --prefix /tmp/stream-monitor-ui-check --no-audit --no-fund jsdom
NODE_PATH=/tmp/stream-monitor-ui-check/node_modules node tests/monitor_dom_smoke.cjs
```

Before deployment, run the image's native Nginx configuration check shown above
and verify actual browser playback and IP fidelity on the LAN host. Configuration
parsing and DOM tests do not substitute for those deployment checks.
