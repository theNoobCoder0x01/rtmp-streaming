# rtmp-streaming
Streaming to a local network from OBS using NGINX-RTMP, with a password on
both sides:

- **Streaming (OBS):** the stream key must be `<name>?pwd=<STREAM_KEY>`, otherwise the stream is refused.
- **Watching (browser):** the player pages, HLS video, dashboard, API and
  WebSocket on port `8080` all ask for a username and password.

Passwords are **not** stored in the image. The container reads them from
environment variables when it starts, so the same image works on any server.

## Setup

1. Create your password file from the template and fill it in:
   ```
   cp stream.env.example stream.env
   ```
   ```
   STREAM_KEY=<long random value, e.g. from: openssl rand -hex 24>
   VIEWER_USER=viewer
   VIEWER_PASSWORD=<viewer password>
   ```
   `stream.env` is git-ignored. Keep it private.

2. Start it:
   ```
   docker compose up --build -d
   ```
   If `stream.env` is missing or incomplete, the container stops with a
   message saying what's missing. It never starts unprotected.

3. In OBS, use `rtmp://<host-ip>:1935/live` as the server and
   `<name>?pwd=<STREAM_KEY>` as the stream key (for example `test?pwd=...`).

4. Open `http://<host-ip>:8080/` (or `/m.html`, `/player.html`) and log in.

To change a password, edit `stream.env` and run `docker compose up -d` again.
No rebuild needed.

Stop the service with:

```
docker compose down
```

## Variables

| Variable | Required | Meaning |
|---|---|---|
| `STREAM_KEY` | yes | OBS stream password. Letters, digits, `-` and `_` only. |
| `VIEWER_PASSWORD` | yes* | Browser password. Hashed when the container starts. |
| `VIEWER_PASSWORD_HASH` | yes* | Use instead of `VIEWER_PASSWORD` to avoid keeping the plain password on the server (create with `openssl passwd -6`). |
| `VIEWER_USER` | no | Browser username, default `viewer`. |

\* Set one of the two.

Alternative to env vars: run `./make-secrets.sh viewer` to create a `secrets/`
folder and mount it at `/etc/nginx/secrets` instead.

## Ports

- `1935`: RTMP (OBS)
- `8080`: web pages, HLS, dashboard, `/api` and `/ws`, all behind the login

The API server listens on `127.0.0.1:8000` inside the container only. The
dashboard reaches it through nginx on `8080`, so it is protected by the same
login.

> Plain HTTP/RTMP sends passwords unencrypted. Acceptable on a trusted network
> only. Add HTTPS/RTMPS before exposing this to the internet.
