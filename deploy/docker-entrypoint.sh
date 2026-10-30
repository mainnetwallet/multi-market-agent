#!/bin/sh
# Render mounts a persistent disk at /app/data, commonly root-owned. Start as
# root only long enough to make the data directory writable, then drop to the
# non-root application user for the actual server process.
set -eu

if [ "$(id -u)" = "0" ]; then
    mkdir -p /app/data
    chown -R mmagent:mmagent /app/data 2>/dev/null || true
    exec setpriv --reuid=10001 --regid=10001 --init-groups "$@"
fi

exec "$@"
