-- Runs only once, when the database volume is first created.
-- The dev database "docsum" is created by POSTGRES_DB in docker-compose.yml.
-- pytest uses this separate database and empties its tables before every test.
CREATE DATABASE docsum_test;
-- The eval runner (python -m evals.run_evals) indexes its corpus here. It also creates this
-- database itself if missing, for volumes created before this line existed.
CREATE DATABASE docsum_eval;
