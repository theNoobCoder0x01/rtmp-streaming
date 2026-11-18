#!/usr/bin/env sh
# Creates secrets/ with a hashed viewer password.
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

chmod 644 secrets/.htpasswd   # nginx worker (non-root) must read it; it only holds a hash

echo "Done. Mount secrets/ at /etc/nginx/secrets to use the viewer login."
