#!/usr/bin/env bash
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
	CREATE USER convmem with password 'convmem';
	CREATE DATABASE convmem;
	GRANT ALL PRIVILEGES ON DATABASE convmem TO convmem;
EOSQL