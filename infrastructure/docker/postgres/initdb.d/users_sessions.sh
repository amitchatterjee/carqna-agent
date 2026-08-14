#!/usr/bin/env bash
set -e

# Named "users_sessions.sh" (not "user_sessions.sh") specifically so it sorts
# alphabetically after "users_registry.sh" -- ASCII '_' (0x5F) sorts before
# 's' (0x73), so "user_sessions.sh" would incorrectly sort *before*
# "users_registry.sh" and run before the user_registry table this one's
# foreign key depends on exists. docker-entrypoint-initdb.d scripts run in
# filename order, only on a fresh/empty data volume.
#
# Connect as carqna itself, not $POSTGRES_USER, so the table ends up
# carqna-owned -- see users_registry.sh's own comment for why (an earlier
# version of that script connected as the postgres superuser and caused
# `permission denied` for both the app and manual psql queries).
psql -v ON_ERROR_STOP=1 --username "carqna" --dbname "carqna" <<-EOSQL
	CREATE TABLE IF NOT EXISTS user_sessions (
	    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
	    user_registry_id BIGINT NOT NULL REFERENCES user_registry(id),
	    session_name VARCHAR(256) NOT NULL,
	    access_ts TIMESTAMPTZ NOT NULL DEFAULT now(),
	    UNIQUE (user_registry_id, session_name)
	);
	CREATE INDEX IF NOT EXISTS idx_user_sessions_user_registry_id ON user_sessions(user_registry_id);
EOSQL
