# Monitoring validation

Implemented on `feature/lan-monitoring`, based on `add-password-auth` at `c84e9fe`.

## Passed

- 58 Python tests: session/event persistence, duplicate/concurrent callbacks,
  reconnects, IP identity, HLS inactivity/logout/queued writes, restart recovery,
  outage grace, late-snapshot races, report interval boundaries and peaks,
  filtering/pagination, CSV escaping, API authorization/private-route isolation,
  existing login/refresh/WebSocket and startup regressions.
- 11 Node tests: stable dropdown selection/options/playback, initial selection,
  reconnection, offline choices, and report date/duration helpers.
- DOM smoke test using SQLite-generated fixtures: Live, Sessions, Events and
  Reports rendering, role/IP filters, CSV actions, empty/error/recovery states,
  chart creation, and safe text rendering of malicious-looking stream names.
- JavaScript syntax and Python 3.7 syntax compatibility checks.
- Docker Compose validation, persistent-volume mapping and exposed-port checks.
- Nginx configuration parsing with Crossplane, including argument/context checks
  supported by that parser.
- Git whitespace checks.

## Not verified in this environment

Native Chrome startup was blocked by host permissions. DOM testing does not
verify browser painting/layout, actual media playback or browser networking.
The temporary native Nginx/RTMP validation binary built successfully, but the
host blocked its initial networking sysctl access before `nginx -t` could parse
the configuration. Crossplane parsing is not a substitute for native module
validation.

Before deployment, use the README's container preflight (`nginx -t`), then verify
actual publish/play start/end events, browser playback and two distinct LAN
device IPs on the target host. No streaming service was started during this work.

## Accuracy boundaries

RTMP callbacks are exact observations; polling recovery and browser inactivity
ends are explicitly marked as inferred. HLS touches are coalesced within five
seconds; inactivity is 30 seconds. Raw auth tokens are not persisted. A shared
account/IP does not identify individual people. Short connections entirely inside
a monitoring outage cannot be reconstructed. Network NAT/proxies can mask source
IPs. Report durations/peak counts include inferred session intervals where
applicable; RTMP bandwidth excludes HLS HTTP egress.
