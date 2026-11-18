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
