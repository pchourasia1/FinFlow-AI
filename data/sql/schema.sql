-- FinFlow-AI database schema
-- Mirrors the live Supabase schema shown in docs/images/Tables_Schema.png.
-- Run once in the Supabase SQL editor (or psql) before loading data.
-- Safe to re-run: every statement uses IF NOT EXISTS.

-- pgvector, for policy_document.embedding
CREATE EXTENSION IF NOT EXISTS vector;


-- Users (data/synthetic/users.csv)
CREATE TABLE IF NOT EXISTS users (
    user_id     INT8 PRIMARY KEY,
    name        TEXT,
    email       TEXT,
    phone       TEXT,
    address     TEXT,
    city        TEXT,
    state       TEXT,
    risk_score  FLOAT8
);


-- Accounts (data/synthetic/accounts.csv)
CREATE TABLE IF NOT EXISTS accounts (
    account_id  INT8 PRIMARY KEY,
    user_id     INT8 REFERENCES users (user_id),
    balance     FLOAT8,
    type        TEXT
);


-- Transactions (data/synthetic/transactions.csv)
-- Same definition load_transactions.py creates.
CREATE TABLE IF NOT EXISTS transactions (
    transaction_id   BIGSERIAL PRIMARY KEY,
    account_id       INT4 NOT NULL,
    amount           NUMERIC(15,2) NOT NULL,
    category         VARCHAR(50) NOT NULL,
    merchant         VARCHAR(255),
    timestamp        TIMESTAMP NOT NULL,
    running_balance  NUMERIC(15,2),
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_account
        FOREIGN KEY (account_id)
        REFERENCES accounts (account_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_transactions_account_id        ON transactions (account_id);
CREATE INDEX IF NOT EXISTS idx_transactions_timestamp         ON transactions (timestamp);
CREATE INDEX IF NOT EXISTS idx_transactions_account_timestamp ON transactions (account_id, timestamp);


-- Policy document chunks (filled by data/scripts/vectorize.py)
-- 384 dimensions = sentence-transformers/all-MiniLM-L6-v2
CREATE TABLE IF NOT EXISTS policy_document (
    id          SERIAL PRIMARY KEY,
    title       VARCHAR NOT NULL,
    content     TEXT NOT NULL,
    embedding   vector(384),
    metadata    JSONB,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);


-- Loading the CSVs
-- Supabase dashboard: Table Editor -> users -> Insert -> Import data from CSV,
-- then the same for accounts. Load users first (accounts references it).
--
-- Or from psql, run from the project root:
--   \copy users    FROM 'data/synthetic/users.csv'    WITH (FORMAT csv, HEADER true)
--   \copy accounts FROM 'data/synthetic/accounts.csv' WITH (FORMAT csv, HEADER true)
--
-- Then: python data/scripts/load_transactions.py
--       python data/scripts/vectorize.py
