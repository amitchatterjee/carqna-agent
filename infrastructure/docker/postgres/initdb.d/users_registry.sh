#!/usr/bin/env bash
set -e

psql -v ON_ERROR_STOP=1 --username "convmem" --dbname "convmem" <<-EOSQL
	CREATE TABLE IF NOT EXISTS user_registry (
	    user_id TEXT PRIMARY KEY,
	    email TEXT,
	    name TEXT,
	    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
	    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
	);
EOSQL
