-- Runs once, when Postgres starts with an empty data folder. MLflow gets its own database and
-- user, so it can't read or change the platform's tables (cases, audit log). Dev password only.
CREATE USER mlflow WITH PASSWORD 'mlflow_dev_only';
CREATE DATABASE mlflow OWNER mlflow;
