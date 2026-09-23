#!/bin/bash
# usage: plexdb.sh "SQL"  — runs against lab Plex DB with Plex SQLite
docker exec mlab-plex "/usr/lib/plexmediaserver/Plex SQLite" "/config/Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db" "$@"
