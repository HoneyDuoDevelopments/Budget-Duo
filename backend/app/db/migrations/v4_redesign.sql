-- ============================================================
-- Budget Duo V4 Migration
-- Run: docker exec -i budget-duo-db psql -U budget_duo -d budget_duo < v4_redesign.sql
-- ============================================================

BEGIN;

-- ============================================================
-- 1. ACCOUNTS — add exclude_from_savings flag
-- ============================================================

ALTER TABLE accounts ADD COLUMN IF NOT EXISTS exclude_from_savings BOOLEAN DEFAULT FALSE;

-- Mark 0687 (joint BofA bills-only savings) — outflows fold into expenses, never counted as savings
UPDATE accounts SET exclude_from_savings = TRUE WHERE last_four = '0687';

-- Also ensure is_bills_only is set correctly on 0687
UPDATE accounts SET is_bills_only = TRUE WHERE last_four = '0687';

-- ============================================================
-- 2. TRANSACTIONS — migrate category columns to 4-level tree
-- ============================================================

-- Add new category level columns (nullable, filled manually going forward)
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS cat_l1 VARCHAR(255) REFERENCES categories(id) ON DELETE SET NULL;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS cat_l2 VARCHAR(255) REFERENCES categories(id) ON DELETE SET NULL;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS cat_l3 VARCHAR(255) REFERENCES categories(id) ON DELETE SET NULL;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS cat_l4 VARCHAR(255) REFERENCES categories(id) ON DELETE SET NULL;

-- Remap old txn_class values to new v4 classes
-- Old: savings_move → new: savings_in or savings_out based on amount
UPDATE transactions
SET txn_class = CASE
    WHEN amount > 0 THEN 'savings_in'
    ELSE 'savings_out'
END
WHERE txn_class = 'savings_move';

-- Old: debt_payment → keep as expense (mortgage/truck come from 0687 which we handle separately)
UPDATE transactions
SET txn_class = 'expense'
WHERE txn_class = 'debt_payment';

-- Transactions from 0687 (bills-only account) — reclassify as expense so they show on transactions page
UPDATE transactions
SET txn_class = 'expense'
WHERE account_id = (SELECT id FROM accounts WHERE last_four = '0687')
  AND txn_class IN ('internal_transfer', 'savings_move', NULL)
  AND amount < 0;

-- Null out old category assignments — starting fresh with new taxonomy
UPDATE transactions SET
    category_id = NULL,
    subcategory_id = NULL,
    cat_l1 = NULL,
    cat_l2 = NULL,
    cat_l3 = NULL,
    cat_l4 = NULL,
    user_verified = FALSE
WHERE date >= '2026-01-01';

-- Add indexes for new category columns
CREATE INDEX IF NOT EXISTS ix_transactions_cat_l1 ON transactions(cat_l1);
CREATE INDEX IF NOT EXISTS ix_transactions_cat_l2 ON transactions(cat_l2);
CREATE INDEX IF NOT EXISTS ix_transactions_cat_l3 ON transactions(cat_l3);
CREATE INDEX IF NOT EXISTS ix_transactions_cat_l4 ON transactions(cat_l4);

-- ============================================================
-- 3. CATEGORIES — drop old system categories, rebuild taxonomy
-- ============================================================

-- Remove FK constraints temporarily so we can clean up
ALTER TABLE transactions DROP CONSTRAINT IF EXISTS transactions_category_id_fkey;
ALTER TABLE transactions DROP CONSTRAINT IF EXISTS transactions_subcategory_id_fkey;

-- Clear old category assignments
UPDATE transactions SET category_id = NULL, subcategory_id = NULL;

-- Remove all old system categories (user categories preserved if any exist)
DELETE FROM categories WHERE is_system = TRUE;

-- Add new columns to categories table
ALTER TABLE categories ADD COLUMN IF NOT EXISTS level SMALLINT DEFAULT 1;
ALTER TABLE categories ADD COLUMN IF NOT EXISTS is_dynamic_parent BOOLEAN DEFAULT FALSE;
ALTER TABLE categories ADD COLUMN IF NOT EXISTS entry_type VARCHAR(32) DEFAULT NULL; -- 'car' | 'project' | 'trip'

-- ============================================================
-- 4. DYNAMIC ENTRIES TABLE (cars, projects, trips)
-- ============================================================

CREATE TABLE IF NOT EXISTS dynamic_entries (
    id          VARCHAR(255) PRIMARY KEY,
    entry_type  VARCHAR(32)  NOT NULL,  -- 'car' | 'project' | 'trip'
    name        VARCHAR(255) NOT NULL,
    description TEXT,
    is_active   BOOLEAN DEFAULT TRUE,
    sort_order  INTEGER DEFAULT 50,
    created_at  TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_dynamic_entries_type ON dynamic_entries(entry_type);

-- Seed initial cars
INSERT INTO dynamic_entries (id, entry_type, name, sort_order) VALUES
    ('car_4runner',  'car',  '4Runner', 10),
    ('car_f250',     'car',  'F250',    20),
    ('car_neon',     'car',  'Neon',    30),
    ('car_miata',    'car',  'Miata',   40)
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- 5. SEED NEW CATEGORY TAXONOMY
-- ============================================================
-- Level 1 = Main Type (L1)
-- Level 2 = Sub Type 1 (L2)
-- Level 3 = Sub Type 2 (L3)
-- Level 4 = Sub Type 3 / Dynamic (L4)

-- ── L1: INCOME ──
INSERT INTO categories (id, name, parent_id, level, color, is_system, sort_order) VALUES
    ('inc',             'Income',           NULL, 1, '#00e5a0', TRUE, 10);

-- L2 under Income
INSERT INTO categories (id, name, parent_id, level, color, is_system, sort_order) VALUES
    ('inc_work',        'Work',             'inc',  2, '#00e5a0', TRUE, 10),
    ('inc_business',    'Business',         'inc',  2, '#3d9eff', TRUE, 20),
    ('inc_investment',  'Investment',       'inc',  2, '#b388ff', TRUE, 30);

-- L3 under Income > Business
INSERT INTO categories (id, name, parent_id, level, color, is_system, sort_order) VALUES
    ('inc_biz_3dp',     '3D Printing',      'inc_business', 3, '#3d9eff', TRUE, 10),
    ('inc_biz_sticker', 'Sticker Duo',      'inc_business', 3, '#3d9eff', TRUE, 20),
    ('inc_biz_laser',   'Laser Engraving',  'inc_business', 3, '#3d9eff', TRUE, 30);

-- ── L1: EXPENSE ──
INSERT INTO categories (id, name, parent_id, level, color, is_system, sort_order) VALUES
    ('exp',             'Expense',          NULL, 1, '#ff5e7a', TRUE, 20);

-- L2 under Expense
INSERT INTO categories (id, name, parent_id, level, color, is_system, sort_order) VALUES
    ('exp_utilities',   'Utilities',        'exp', 2, '#00d4e8', TRUE, 10),
    ('exp_household',   'Household',        'exp', 2, '#3d9eff', TRUE, 20),
    ('exp_cars',        'Cars',             'exp', 2, '#ffb340', TRUE, 30),
    ('exp_business',    'Business',         'exp', 2, '#b388ff', TRUE, 40),
    ('exp_mortgage',    'Mortgage',         'exp', 2, '#ff5e7a', TRUE, 50);

-- L3 under Expense > Utilities
INSERT INTO categories (id, name, parent_id, level, color, is_system, sort_order) VALUES
    ('exp_util_water',    'Water',           'exp_utilities', 3, '#00d4e8', TRUE, 10),
    ('exp_util_electric', 'Electrical & Gas','exp_utilities', 3, '#00d4e8', TRUE, 20),
    ('exp_util_trash',    'Trash',           'exp_utilities', 3, '#00d4e8', TRUE, 30),
    ('exp_util_sewer',    'Sewer',           'exp_utilities', 3, '#00d4e8', TRUE, 40),
    ('exp_util_internet', 'Internet / Phones','exp_utilities',3, '#00d4e8', TRUE, 50);

-- L3 under Expense > Household
INSERT INTO categories (id, name, parent_id, level, color, is_system, sort_order) VALUES
    ('exp_hh_food',       'Food & Drink',     'exp_household', 3, '#3d9eff', TRUE, 10),
    ('exp_hh_home_imp',   'Home Improvement', 'exp_household', 3, '#3d9eff', TRUE, 20),
    ('exp_hh_clothes',    'Clothes',          'exp_household', 3, '#3d9eff', TRUE, 30),
    ('exp_hh_family_fun', 'Family Fun',       'exp_household', 3, '#3d9eff', TRUE, 40),
    ('exp_hh_kids',       'Money to Kids',    'exp_household', 3, '#3d9eff', TRUE, 50);

-- L3 under Expense > Cars (dynamic parent — L4 entries come from dynamic_entries)
UPDATE categories SET is_dynamic_parent = TRUE, entry_type = 'car'
WHERE id = 'exp_cars';

INSERT INTO categories (id, name, parent_id, level, color, is_system, sort_order) VALUES
    ('exp_cars_payment',  'Car Payments',   'exp_cars', 3, '#ffb340', TRUE, 10),
    ('exp_cars_insurance','Car Insurance',  'exp_cars', 3, '#ffb340', TRUE, 20),
    ('exp_cars_maint',    'Maintenance',    'exp_cars', 3, '#ffb340', TRUE, 30),
    ('exp_cars_reg',      'Registration',   'exp_cars', 3, '#ffb340', TRUE, 40),
    ('exp_cars_smog',     'Smog',           'exp_cars', 3, '#ffb340', TRUE, 50),
    ('exp_cars_tires',    'Tires',          'exp_cars', 3, '#ffb340', TRUE, 60);

-- L3 under Expense > Business
INSERT INTO categories (id, name, parent_id, level, color, is_system, sort_order) VALUES
    ('exp_biz_3dp',     '3D Printing',      'exp_business', 3, '#b388ff', TRUE, 10),
    ('exp_biz_sticker', 'Sticker Duo',      'exp_business', 3, '#b388ff', TRUE, 20),
    ('exp_biz_laser',   'Laser Engraving',  'exp_business', 3, '#b388ff', TRUE, 30);

-- L4 under Expense > Household > Food & Drink
INSERT INTO categories (id, name, parent_id, level, color, is_system, sort_order) VALUES
    ('exp_food_grocery',  'Groceries',      'exp_hh_food', 4, '#3d9eff', TRUE, 10),
    ('exp_food_drinks',   'Drinks',         'exp_hh_food', 4, '#3d9eff', TRUE, 20),
    ('exp_food_dining',   'Dining Out',     'exp_hh_food', 4, '#3d9eff', TRUE, 30);

-- L4 under Expense > Household > Home Improvement (dynamic parent — L4 entries come from dynamic_entries)
UPDATE categories SET is_dynamic_parent = TRUE, entry_type = 'project'
WHERE id = 'exp_hh_home_imp';

-- L4 under Expense > Household > Family Fun
INSERT INTO categories (id, name, parent_id, level, color, is_system, sort_order) VALUES
    ('exp_fun_concerts',  'Concerts',       'exp_hh_family_fun', 4, '#3d9eff', TRUE, 10),
    ('exp_fun_vacations', 'Vacations',      'exp_hh_family_fun', 4, '#3d9eff', TRUE, 20),
    ('exp_fun_streaming', 'Streaming',      'exp_hh_family_fun', 4, '#3d9eff', TRUE, 30),
    ('exp_fun_gaming',    'Gaming',         'exp_hh_family_fun', 4, '#3d9eff', TRUE, 40),
    ('exp_fun_nights_out','Nights Out',     'exp_hh_family_fun', 4, '#3d9eff', TRUE, 50),
    ('exp_fun_entertain', 'Entertainment',  'exp_hh_family_fun', 4, '#3d9eff', TRUE, 60);

-- Vacations is a dynamic parent (trips)
UPDATE categories SET is_dynamic_parent = TRUE, entry_type = 'trip'
WHERE id = 'exp_fun_vacations';

-- ── L1: SAVINGS IN ──
INSERT INTO categories (id, name, parent_id, level, color, is_system, sort_order) VALUES
    ('sav_in',          'Savings In',       NULL, 1, '#00e5a0', TRUE, 30);

-- ── L1: SAVINGS OUT ──
INSERT INTO categories (id, name, parent_id, level, color, is_system, sort_order) VALUES
    ('sav_out',         'Savings Out',      NULL, 1, '#ffb340', TRUE, 40);

-- ── L1: INVESTMENT IN ──
INSERT INTO categories (id, name, parent_id, level, color, is_system, sort_order) VALUES
    ('inv_in',          'Investment In',    NULL, 1, '#b388ff', TRUE, 50);

-- ── L1: INVESTMENT OUT ──
INSERT INTO categories (id, name, parent_id, level, color, is_system, sort_order) VALUES
    ('inv_out',         'Investment Out',   NULL, 1, '#b388ff', TRUE, 60);

-- ── L1: SUBSCRIPTION ──
INSERT INTO categories (id, name, parent_id, level, color, is_system, sort_order) VALUES
    ('sub',             'Subscription',     NULL, 1, '#ffb340', TRUE, 70);

-- L2 under Subscription
INSERT INTO categories (id, name, parent_id, level, color, is_system, sort_order) VALUES
    ('sub_productivity', 'Productivity',    'sub', 2, '#ffb340', TRUE, 10),
    ('sub_streaming',    'Streaming',       'sub', 2, '#ffb340', TRUE, 20),
    ('sub_services',     'Services',        'sub', 2, '#ffb340', TRUE, 30),
    ('sub_music',        'Music',           'sub', 2, '#ffb340', TRUE, 40);

-- ── L1: TRANSFER (internal — not displayed in spending, just for reference) ──
INSERT INTO categories (id, name, parent_id, level, color, is_system, sort_order) VALUES
    ('transfer',        'Transfer',         NULL, 1, '#3d5268', TRUE, 80),
    ('cc_payment',      'CC Payment',       NULL, 1, '#3d5268', TRUE, 90),
    ('ignore',          'Ignore',           NULL, 1, '#3d5268', TRUE, 100);

-- ============================================================
-- 6. RE-ADD FK CONSTRAINTS for old columns (kept for compat)
-- ============================================================

-- Old category_id / subcategory_id kept as nullable text — no FK needed going forward
-- New cat_l1..l4 already have FK constraints added in step 2

-- ============================================================
-- 7. REBUILD MERCHANT RULES TABLE
-- ============================================================

-- Drop and recreate clean — no user rules exist yet worth keeping
DROP TABLE IF EXISTS merchant_rules CASCADE;

CREATE TABLE merchant_rules (
    id              VARCHAR(255) PRIMARY KEY,
    match_type      VARCHAR(32)  NOT NULL,  -- description_contains | description_starts_with | counterparty_exact
    match_value     VARCHAR(512) NOT NULL,
    txn_class       VARCHAR(32),
    cat_l1          VARCHAR(255) REFERENCES categories(id) ON DELETE SET NULL,
    cat_l2          VARCHAR(255) REFERENCES categories(id) ON DELETE SET NULL,
    cat_l3          VARCHAR(255) REFERENCES categories(id) ON DELETE SET NULL,
    cat_l4          VARCHAR(255) REFERENCES categories(id) ON DELETE SET NULL,
    recurring_type  VARCHAR(32),
    merchant_clean  VARCHAR(255),
    priority        INTEGER DEFAULT 100,
    is_system       BOOLEAN DEFAULT FALSE,
    is_active       BOOLEAN DEFAULT TRUE,
    match_count     INTEGER DEFAULT 0,
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX ix_merchant_rules_match_type ON merchant_rules(match_type);
CREATE INDEX ix_merchant_rules_active     ON merchant_rules(is_active);

-- Seed a handful of obvious system rules to get started
INSERT INTO merchant_rules (id, match_type, match_value, txn_class, recurring_type, merchant_clean, is_system, priority) VALUES
    ('sys_tesla_payroll',   'description_contains',  'tesla motors',  'income',   NULL,           'Tesla Payroll',    TRUE, 10),
    ('sys_payroll',         'description_contains',  'payroll',       'income',   NULL,           'Payroll',          TRUE, 10),
    ('sys_direct_dep',      'description_contains',  'direct dep',    'income',   NULL,           'Direct Deposit',   TRUE, 15),
    ('sys_zelle',           'description_contains',  'zelle',         'internal_transfer', NULL,  'Zelle',            TRUE, 20),
    ('sys_venmo',           'description_contains',  'venmo',         'internal_transfer', NULL,  'Venmo',            TRUE, 20),
    ('sys_apple_pay_xfer',  'description_contains',  'apple cash',    'internal_transfer', NULL,  'Apple Cash',       TRUE, 20),
    ('sys_cc_payment',      'description_contains',  'payment thank', 'cc_payment', NULL,         'CC Payment',       TRUE, 25),
    ('sys_cc_autopay',      'description_contains',  'autopay',       'cc_payment', NULL,         'CC Autopay',       TRUE, 25),
    ('sys_netflix',         'description_contains',  'netflix',       'expense',  'subscription', 'Netflix',          TRUE, 50),
    ('sys_spotify',         'description_contains',  'spotify',       'expense',  'subscription', 'Spotify',          TRUE, 50),
    ('sys_hulu',            'description_contains',  'hulu',          'expense',  'subscription', 'Hulu',             TRUE, 50),
    ('sys_disney',          'description_contains',  'disney plus',   'expense',  'subscription', 'Disney+',          TRUE, 50),
    ('sys_amazon_prime',    'description_contains',  'amazon prime',  'expense',  'subscription', 'Amazon Prime',     TRUE, 50),
    ('sys_apple_one',       'description_contains',  'apple.com/bill','expense',  'subscription', 'Apple',            TRUE, 50),
    ('sys_youtube',         'description_contains',  'youtube',       'expense',  'subscription', 'YouTube',          TRUE, 50),
    ('sys_chatgpt',         'description_contains',  'openai',        'expense',  'subscription', 'ChatGPT',          TRUE, 50),
    ('sys_amazon_purchase', 'description_contains',  'amazon',        'expense',  NULL,           'Amazon',           TRUE, 80),
    ('sys_walmart',         'description_contains',  'walmart',       'expense',  NULL,           'Walmart',          TRUE, 80),
    ('sys_target',          'description_contains',  'target',        'expense',  NULL,           'Target',           TRUE, 80),
    ('sys_costco',          'description_contains',  'costco',        'expense',  NULL,           'Costco',           TRUE, 80)
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- 8. VERIFY
-- ============================================================

SELECT 'accounts with exclude_from_savings' as check, COUNT(*) FROM accounts WHERE exclude_from_savings = TRUE;
SELECT 'total categories seeded' as check, COUNT(*) FROM categories;
SELECT 'dynamic_entries seeded' as check, COUNT(*) FROM dynamic_entries;
SELECT 'merchant_rules seeded' as check, COUNT(*) FROM merchant_rules;
SELECT 'transactions since 2026-01-01' as check, COUNT(*) FROM transactions WHERE date >= '2026-01-01';

COMMIT;