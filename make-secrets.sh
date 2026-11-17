#!/usr/bin/env sh
# Creates secrets/ with a random OBS stream key and a hashed viewer password.
# Usage: ./make-secrets.sh <viewer-username>
set -eu
user="${1:-viewer}"
mkdir -p secrets

printf "Viewer password for '%s': " "$user"
stty -echo 2>/dev/null || true; read -r pass; stty echo 2>/dev/null || true; echo

# Store only a hash, never the plain password. SHA-512 crypt where OpenSSL
# supports it; macOS's built-in LibreSSL doesn't, so fall back to apr1.
if echo x | openssl passwd -6 -stdin >/dev/null 2>&1; then algo=-6; else algo=-apr1; fi
hash="$(printf '%s\n' "$pass" | openssl passwd "$algo" -stdin)"
printf '%s:%s\n' "$user" "$hash" > secrets/.htpasswd

key="$(openssl rand -hex 24)"
printf 'map $arg_pwd $publish_ok { default 0; "%s" 1; }\n' "$key" > secrets/publish_key.conf
chmod 600 secrets/publish_key.conf
chmod 644 secrets/.htpasswd   # nginx worker (non-root) must read it; it only holds a hash

echo "Done. In OBS set the stream key to:  test?pwd=$key"
