-- ============================================================
-- Budget Duo V5 Migration — Scraper Support
-- Run: docker exec -i budget-duo-db psql -U budget_duo -d budget_duo < backend/app/db/migrations/v5_scraper_migration.sql
-- ============================================================

BEGIN;

-- ============================================================
-- 1. SCRAPER SESSIONS — tracks active scraper runs + 2FA handshake
-- ============================================================

CREATE TABLE IF NOT EXISTS scraper_sessions (
    id              VARCHAR(255) PRIMARY KEY,
    provider        VARCHAR(32)  NOT NULL,    -- 'apple' | 'synchrony'
    status          VARCHAR(32)  NOT NULL,    -- starting | logging_in | awaiting_2fa | verifying_2fa | authenticated | scraping_balance | scraping_transactions | importing | complete | error
    started_by      VARCHAR(32)  DEFAULT 'manual',  -- 'manual' | 'scheduled'
    balance_data    TEXT,                     -- JSON blob: {"balance": ..., "available": ...}
    txn_count       INTEGER      DEFAULT 0,
    error_message   TEXT,
    started_at      TIMESTAMP    DEFAULT NOW(),
    completed_at    TIMESTAMP,
    updated_at      TIMESTAMP    DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_scraper_sessions_provider ON scraper_sessions(provider);
CREATE INDEX IF NOT EXISTS ix_scraper_sessions_status   ON scraper_sessions(status);

-- ============================================================
-- 2. TRANSACTIONS — add import_source to track where data came from
-- ============================================================

ALTER TABLE transactions ADD COLUMN IF NOT EXISTS import_source VARCHAR(32) DEFAULT 'teller';
-- Values: 'teller' | 'apple_csv' | 'synchrony_scrape' | 'manual'

CREATE INDEX IF NOT EXISTS ix_transactions_import_source ON transactions(import_source);

-- Backfill existing transactions as teller-sourced
UPDATE transactions SET import_source = 'teller' WHERE import_source IS NULL;

-- ============================================================
-- 3. MANUAL ENROLLMENT for scraped accounts
-- These accounts aren't in Teller — we create them manually
-- so transactions can link to them via the existing FK structure
-- ============================================================

-- Scraper enrollment (placeholder — no real Teller access token)
INSERT INTO enrollments (id, institution_id, institution_name, owner, access_token, status)
VALUES ('enr_scraper_apple', 'apple_card', 'Apple Card (Goldman Sachs)', 'sam', 'scraper_managed', 'active')
ON CONFLICT (id) DO NOTHING;

INSERT INTO enrollments (id, institution_id, institution_name, owner, access_token, status)
VALUES ('enr_scraper_synchrony', 'synchrony', 'Synchrony Bank', 'sam', 'scraper_managed', 'active')
ON CONFLICT (id) DO NOTHING;

-- Apple Card account
INSERT INTO accounts (id, enrollment_id, institution_name, name, type, subtype, last_four, owner, role, status)
VALUES ('acc_scraper_apple_card', 'enr_scraper_apple', 'Apple Card', 'Apple Card', 'credit', 'credit_card', '8983', 'sam', 'credit', 'open')
ON CONFLICT (id) DO NOTHING;

-- Synchrony: Discount Tire card (ending 5339)
INSERT INTO accounts (id, enrollment_id, institution_name, name, type, subtype, last_four, owner, role, status)
VALUES ('acc_scraper_sync_5339', 'enr_scraper_synchrony', 'Synchrony Bank', 'Discount Tire / Synchrony Car Care', 'credit', 'credit_card', '5339', 'sam', 'credit', 'open')
ON CONFLICT (id) DO NOTHING;

-- Synchrony: Amazon Prime Store Card (ending 8814)
INSERT INTO accounts (id, enrollment_id, institution_name, name, type, subtype, last_four, owner, role, status)
VALUES ('acc_scraper_sync_8814', 'enr_scraper_synchrony', 'Synchrony Bank', 'Amazon Prime Store Card', 'credit', 'credit_card', '8814', 'sam', 'credit', 'open')
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- 4. VERIFY
-- ============================================================

SELECT 'scraper_sessions table' as check_item,
       (SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'scraper_sessions') as exists;

SELECT 'import_source column' as check_item,
       (SELECT COUNT(*) FROM information_schema.columns WHERE table_name = 'transactions' AND column_name = 'import_source') as exists;

SELECT 'scraper accounts' as check_item, COUNT(*) as count
FROM accounts WHERE id LIKE 'acc_scraper_%';

SELECT 'scraper enrollments' as check_item, COUNT(*) as count
FROM enrollments WHERE id LIKE 'enr_scraper_%';

COMMIT;