-- Runs only once, when the database volume is first created.
-- The dev database "docsum" is created by POSTGRES_DB in docker-compose.yml.
-- pytest uses this separate database and empties its tables before every test.
CREATE DATABASE docsum_test;
