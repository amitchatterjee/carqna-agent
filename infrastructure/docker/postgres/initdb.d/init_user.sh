#!/usr/bin/env bash
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
	CREATE USER carqna with password 'carqna';
	CREATE DATABASE carqna;
	GRANT ALL PRIVILEGES ON DATABASE carqna TO carqna;
EOSQL

# GRANT ON SCHEMA public is scoped to the currently-connected database, so it
# can't be combined with CREATE DATABASE in the session above -- PG15+ no
# longer grants CREATE on `public` to PUBLIC by default, and that revocation
# is per-database, so this needs its own session connected to carqna itself.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "carqna" <<-EOSQL
	GRANT ALL ON SCHEMA public TO carqna;
EOSQL