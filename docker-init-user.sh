#!/usr/bin/with-contenv bash
# shellcheck shell=bash
# Simplified init-adduser without branding
# GPU device groups are handled by the base image's init-device-perms
# service via ATTACHED_DEVICES_PERMS (set in Dockerfile).

PUID=${PUID:-1000}
PGID=${PGID:-1000}

if [[ -z ${LSIO_READ_ONLY_FS} ]] && [[ -z ${LSIO_NON_ROOT_USER} ]]; then
    USERHOME=$(grep abc /etc/passwd | cut -d ":" -f6)
    usermod -d "/root" abc
    groupmod -o -g "${PGID}" abc
    usermod -o -u "${PUID}" abc
    usermod -d "${USERHOME}" abc
fi

# Docker creates a missing bind-mount folder, and seeds a new named volume, as root. The app runs as PUID and
# cannot write settings there, so hand over just the top folder when root owns it; anything else is the user's.
if [[ -d /config ]] && [[ "$(stat -c %u /config)" == "0" ]] && [[ "${PUID}" != "0" ]]; then
    chown abc:abc /config
fi
