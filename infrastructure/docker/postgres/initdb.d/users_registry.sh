#!/usr/bin/env bash
set -e

psql -v ON_ERROR_STOP=1 --username "convmem" --dbname "convmem" <<-EOSQL
	CREATE TABLE IF NOT EXISTS user_registry (
	    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
	    user_id TEXT NOT NULL UNIQUE,
	    email TEXT,
	    name TEXT,
	    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
	    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
	);
EOSQL
