#!/usr/bin/env bash
set -e

# Named to sort alphabetically after init_user.sh (which creates the convmem
# database/role these DDLs depend on) -- docker-entrypoint-initdb.d scripts
# run in filename order, only on a fresh/empty data volume.
#
# Connect as convmem itself, not $POSTGRES_USER (the postgres superuser) --
# init_user.sh's `GRANT ALL ON SCHEMA public TO convmem` only covers
# schema-level privileges (CREATE/USAGE), not object-level privileges on a
# table someone else creates. Creating it as convmem makes convmem the owner,
# matching how AsyncPostgresSaver's own checkpoint tables end up owned by
# convmem (created via a connection using convmem's own credentials).
psql -v ON_ERROR_STOP=1 --username "convmem" --dbname "convmem" <<-EOSQL
	CREATE TABLE IF NOT EXISTS users (
	    user_id TEXT PRIMARY KEY,
	    email TEXT,
	    name TEXT,
	    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
	    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
	);
EOSQL
