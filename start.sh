#!/bin/bash
set -euo pipefail

# --- Passwords: built from env vars at start, never stored in the image ------
#   STREAM_KEY            OBS key; stream with key  <name>?pwd=<STREAM_KEY>
#   VIEWER_USER           browser login name (default: viewer)
#   VIEWER_PASSWORD       browser password (hashed here, never stored plain)
#   VIEWER_PASSWORD_HASH  alternative: an already-hashed password
# Or skip the env vars and mount a folder made by make-secrets.sh at
# /etc/nginx/secrets.
dir=/etc/nginx/secrets
user="${VIEWER_USER:-viewer}"

if [ -n "${STREAM_KEY:-}" ] || [ -n "${VIEWER_PASSWORD:-}" ] || [ -n "${VIEWER_PASSWORD_HASH:-}" ]; then
  mkdir -p "$dir"
  if [ -n "${STREAM_KEY:-}" ]; then
    if [[ ! "$STREAM_KEY" =~ ^[A-Za-z0-9_-]+$ ]]; then
      echo "STREAM_KEY may only contain letters, digits, - and _" >&2; exit 1
    fi
    printf 'map $arg_pwd $publish_ok { default 0; "%s" 1; }\n' "$STREAM_KEY" > "$dir/publish_key.conf"
  fi
  if [ -n "${VIEWER_PASSWORD_HASH:-}" ]; then
    printf '%s:%s\n' "$user" "$VIEWER_PASSWORD_HASH" > "$dir/.htpasswd"
  elif [ -n "${VIEWER_PASSWORD:-}" ]; then
    printf '%s:%s\n' "$user" "$(printf '%s\n' "$VIEWER_PASSWORD" | openssl passwd -6 -stdin)" > "$dir/.htpasswd"
  fi
  chmod 644 "$dir/.htpasswd" 2>/dev/null || true
fi

if [ ! -f "$dir/publish_key.conf" ] || [ ! -f "$dir/.htpasswd" ]; then
  echo "Missing passwords. Set STREAM_KEY and VIEWER_PASSWORD (see README)," >&2
  echo "or mount a folder made by make-secrets.sh at $dir." >&2
  exit 1
fi

# --- Services -----------------------------------------------------------------
# Check the config first so a bad setup stops the container instead of
# leaving it running without nginx.
nginx -t
nginx

# Start the Python server
python3 /usr/local/server.py &

# Keep the container running
tail -f /dev/null
