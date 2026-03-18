-- ============================================================
-- Budget Duo V2 Migration
-- Run with:
-- docker compose exec -T db psql -U budget_duo -d budget_duo < migrate_v2.sql
-- ============================================================

BEGIN;

-- ============================================================
-- 1. ADD txn_class TO transactions
-- Classification of what kind of money movement this is
-- ============================================================
ALTER TABLE transactions
  ADD COLUMN IF NOT EXISTS txn_class VARCHAR(32) DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS merchant_clean VARCHAR(255) DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS rule_id VARCHAR(255) DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS category_id VARCHAR(255) DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS subcategory_id VARCHAR(255) DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS recurring_type VARCHAR(32) DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS user_verified BOOLEAN DEFAULT FALSE;

-- Index for fast filtering by class
CREATE INDEX IF NOT EXISTS ix_transactions_txn_class ON transactions(txn_class);
CREATE INDEX IF NOT EXISTS ix_transactions_merchant_clean ON transactions(merchant_clean);
CREATE INDEX IF NOT EXISTS ix_transactions_category_id ON transactions(category_id);

-- ============================================================
-- 2. REBUILD categories table for two-tier system
-- Keep existing data, add budget support
-- ============================================================
ALTER TABLE categories
  ADD COLUMN IF NOT EXISTS budget_amount NUMERIC(12,2) DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS budget_period VARCHAR(16) DEFAULT 'monthly',
  ADD COLUMN IF NOT EXISTS sort_order INTEGER DEFAULT 0,
  ADD COLUMN IF NOT EXISTS exclude_from_spending BOOLEAN DEFAULT FALSE;

-- ============================================================
-- 3. CREATE merchant_rules table
-- The learning engine — rules that auto-classify transactions
-- ============================================================
CREATE TABLE IF NOT EXISTS merchant_rules (
  id VARCHAR(255) PRIMARY KEY,
  -- Matching
  match_type VARCHAR(32) NOT NULL,       -- description_contains | description_starts_with | counterparty_exact | description_regex
  match_value VARCHAR(512) NOT NULL,     -- The string/pattern to match (case-insensitive)
  -- Classification outputs
  txn_class VARCHAR(32),                 -- expense | income | internal_transfer | cc_payment | investment_out | investment_in | debt_payment | savings_move | ignore
  category_id VARCHAR(255),              -- FK to categories
  subcategory_id VARCHAR(255),           -- FK to categories (child)
  recurring_type VARCHAR(32),            -- subscription | utility | recurring_expense | one_time | null
  merchant_clean VARCHAR(255),           -- Normalized merchant name to display
  -- Rule metadata
  priority INTEGER DEFAULT 100,          -- Lower = higher priority. System rules start at 0-50, user rules at 100+
  is_system BOOLEAN DEFAULT FALSE,       -- True = auto-generated, False = user confirmed
  is_active BOOLEAN DEFAULT TRUE,
  match_count INTEGER DEFAULT 0,         -- How many times this rule has fired
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_merchant_rules_match_type ON merchant_rules(match_type);
CREATE INDEX IF NOT EXISTS ix_merchant_rules_active ON merchant_rules(is_active);
CREATE INDEX IF NOT EXISTS ix_merchant_rules_priority ON merchant_rules(priority);

-- ============================================================
-- 4. CREATE account_balances cache table
-- Store the most recent balance fetched from Teller
-- ============================================================
CREATE TABLE IF NOT EXISTS account_balances (
  account_id VARCHAR(255) PRIMARY KEY REFERENCES accounts(id),
  ledger NUMERIC(12,2),
  available NUMERIC(12,2),
  fetched_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ============================================================
-- 5. SEED system merchant rules
-- These cover the patterns we identified in the audit
-- Priority 0-50 = system, never overrides user rules
-- ============================================================

-- Internal bank transfers
INSERT INTO merchant_rules (id, match_type, match_value, txn_class, merchant_clean, priority, is_system)
VALUES
  ('rule_transfer_bofa_online',    'description_contains', 'online banking transfer',     'internal_transfer', 'BofA Internal Transfer',   10, TRUE),
  ('rule_transfer_bofa_payment',   'description_contains', 'online banking payment to crd', 'cc_payment',      'BofA CC Payment',           10, TRUE),
  ('rule_transfer_bofa_sav',       'description_contains', 'online banking transfer to sav', 'savings_move',   'BofA Savings Transfer',     10, TRUE),
  ('rule_transfer_bofa_from_sav',  'description_contains', 'online banking transfer from sav', 'savings_move', 'BofA Savings Transfer',    10, TRUE)
ON CONFLICT (id) DO NOTHING;

-- ETrade / Morgan Stanley deposits
INSERT INTO merchant_rules (id, match_type, match_value, txn_class, merchant_clean, priority, is_system)
VALUES
  ('rule_etrade_mspbna', 'description_contains', 'mspbna', 'investment_in', 'ETrade / Morgan Stanley', 10, TRUE)
ON CONFLICT (id) DO NOTHING;

-- Fidelity Roth IRA contributions
INSERT INTO merchant_rules (id, match_type, match_value, txn_class, merchant_clean, recurring_type, priority, is_system)
VALUES
  ('rule_fidelity_moneyline', 'description_contains', 'fid bkg svc', 'investment_out', 'Fidelity Roth IRA', 'recurring_expense', 10, TRUE)
ON CONFLICT (id) DO NOTHING;

-- Affirm BNPL payments
INSERT INTO merchant_rules (id, match_type, match_value, txn_class, merchant_clean, recurring_type, priority, is_system)
VALUES
  ('rule_affirm_payment', 'description_contains', 'affirm', 'debt_payment', 'Affirm', 'recurring_expense', 10, TRUE)
ON CONFLICT (id) DO NOTHING;

-- Credit card ACH payments from checking
INSERT INTO merchant_rules (id, match_type, match_value, txn_class, merchant_clean, priority, is_system)
VALUES
  ('rule_cc_applecard',    'description_contains', 'applecard gsbank',      'cc_payment', 'Apple Card Payment',    10, TRUE),
  ('rule_cc_citi',         'description_contains', 'citi card online',      'cc_payment', 'Citi Card Payment',     10, TRUE),
  ('rule_cc_capital_one',  'description_contains', 'capital one mobile',    'cc_payment', 'Capital One Payment',   10, TRUE),
  ('rule_cc_amex',         'description_contains', 'american express',      'cc_payment', 'Amex Payment',          10, TRUE),
  ('rule_cc_amazon',       'description_contains', 'amazon corp des:syf',   'cc_payment', 'Amazon Card Payment',   10, TRUE),
  ('rule_cc_home_depot',   'description_contains', 'home depot des:online', 'cc_payment', 'Home Depot Card Payment', 10, TRUE),
  ('rule_cc_interactive',  'description_contains', 'interactive brok',      'investment_out', 'Interactive Brokers', 10, TRUE)
ON CONFLICT (id) DO NOTHING;

-- Credit card on-card payment received entries
INSERT INTO merchant_rules (id, match_type, match_value, txn_class, merchant_clean, priority, is_system)
VALUES
  ('rule_cc_online_payment_received', 'description_contains', 'online payment, thank you', 'cc_payment', 'CC Payment Received', 10, TRUE),
  ('rule_cc_online_payment_from',     'description_contains', 'online payment from chk',   'cc_payment', 'CC Payment Received', 10, TRUE)
ON CONFLICT (id) DO NOTHING;

-- Interest and fees — ignore from spending
INSERT INTO merchant_rules (id, match_type, match_value, txn_class, merchant_clean, priority, is_system)
VALUES
  ('rule_interest_charged', 'description_contains', 'interest charged', 'ignore', 'Interest Charge', 20, TRUE),
  ('rule_monthly_interest',  'description_contains', 'monthly interest',  'ignore', 'Interest',        20, TRUE)
ON CONFLICT (id) DO NOTHING;

-- Tesla payroll - income
INSERT INTO merchant_rules (id, match_type, match_value, txn_class, merchant_clean, priority, is_system)
VALUES
  ('rule_tesla_payroll', 'description_contains', 'tesla motors', 'income', 'Tesla Payroll', 5, TRUE)
ON CONFLICT (id) DO NOTHING;

-- Known subscriptions
INSERT INTO merchant_rules (id, match_type, match_value, txn_class, category_id, merchant_clean, recurring_type, priority, is_system)
VALUES
  ('rule_spotify',      'counterparty_exact',     'Spotify',      'expense', 'entertainment', 'Spotify',         'subscription', 20, TRUE),
  ('rule_disney_plus',  'counterparty_exact',     'Disney Plus',  'expense', 'entertainment', 'Disney+',         'subscription', 20, TRUE),
  ('rule_netflix_1',    'description_contains',   'netflix',      'expense', 'entertainment', 'Netflix',         'subscription', 20, TRUE),
  ('rule_roku_warner',  'description_contains',   'roku for warnerme', 'expense', 'entertainment', 'HBO Max / Roku', 'subscription', 20, TRUE),
  ('rule_claude_ai',    'description_contains',   'claude.ai',    'expense', 'software',      'Claude.ai',       'subscription', 20, TRUE),
  ('rule_ovh',          'description_contains',   'ovh us',       'expense', 'software',      'OVH Hosting',     'subscription', 20, TRUE)
ON CONFLICT (id) DO NOTHING;

-- Known utilities
INSERT INTO merchant_rules (id, match_type, match_value, txn_class, category_id, merchant_clean, recurring_type, priority, is_system)
VALUES
  ('rule_spectrum',      'counterparty_exact',    'Spectrum',                 'expense', 'utilities', 'Spectrum Internet',    'utility', 20, TRUE),
  ('rule_truckee_water', 'description_contains',  'truckee meadows water',    'expense', 'utilities', 'Truckee Meadows Water', 'utility', 20, TRUE),
  ('rule_city_sparks',   'counterparty_exact',    'CITY OF SPARKS',           'expense', 'utilities', 'City of Sparks',        'utility', 20, TRUE),
  ('rule_waste_mgmt',    'description_contains',  'waste management',         'expense', 'utilities', 'Waste Management',      'utility', 20, TRUE)
ON CONFLICT (id) DO NOTHING;

-- Merchant clean names for high-frequency stores
INSERT INTO merchant_rules (id, match_type, match_value, txn_class, category_id, merchant_clean, recurring_type, priority, is_system)
VALUES
  ('rule_costco_store',    'description_contains', 'costco whse',    'expense', 'shopping',  'Costco',      'recurring_expense', 30, TRUE),
  ('rule_costco_gas',      'description_contains', 'costco gas',     'expense', 'fuel',      'Costco Gas',  'recurring_expense', 30, TRUE),
  ('rule_smiths_1',        'description_contains', 'smiths food',    'expense', 'groceries', 'Smith''s',    'recurring_expense', 30, TRUE),
  ('rule_smiths_2',        'counterparty_exact',   'Smith''s Food & Drug Centers', 'expense', 'groceries', 'Smith''s', 'recurring_expense', 30, TRUE),
  ('rule_walmart_1',       'description_contains', 'wal-mart',       'expense', 'shopping',  'Walmart',     'recurring_expense', 30, TRUE),
  ('rule_walmart_2',       'description_contains', 'wm supercenter', 'expense', 'shopping',  'Walmart',     'recurring_expense', 30, TRUE),
  ('rule_walmart_3',       'counterparty_exact',   'Walmart',        'expense', 'shopping',  'Walmart',     'recurring_expense', 30, TRUE),
  ('rule_7eleven_1',       'description_contains', '7-eleven',       'expense', 'fuel',      '7-Eleven',    'recurring_expense', 30, TRUE),
  ('rule_7eleven_2',       'description_contains', '7 eleven',       'expense', 'fuel',      '7-Eleven',    'recurring_expense', 30, TRUE),
  ('rule_chevron_1',       'description_contains', 'chevron',        'expense', 'fuel',      'Chevron',     'recurring_expense', 30, TRUE),
  ('rule_shell_1',         'description_contains', 'shell oil',      'expense', 'fuel',      'Shell',       'recurring_expense', 30, TRUE),
  ('rule_maverik_1',       'description_contains', 'maverik',        'expense', 'fuel',      'Maverik',     'recurring_expense', 30, TRUE),
  ('rule_golden_gate',     'description_contains', 'golden gate usa','expense', 'fuel',      'Golden Gate C Store', 'recurring_expense', 30, TRUE),
  ('rule_market_work',     'description_contains', 'market@work',    'expense', 'dining',    'Market@Work (Cafeteria)', 'recurring_expense', 30, TRUE),
  ('rule_blend_catering',  'description_contains', 'blend catering', 'expense', 'dining',    'Blend Catering (Work)', 'recurring_expense', 30, TRUE),
  ('rule_tyg_xpress',      'description_contains', 'tyg xpress',     'expense', 'dining',    'TYG Xpress (Work)',     'recurring_expense', 30, TRUE),
  ('rule_panda',           'counterparty_exact',   'Panda Express',  'expense', 'dining',    'Panda Express',         'recurring_expense', 30, TRUE),
  ('rule_pizzava',         'description_contains', 'pizzava',        'expense', 'dining',    'Pizzava',               'recurring_expense', 30, TRUE),
  ('rule_home_depot_card', 'description_contains', 'the home depot', 'expense', 'home',      'Home Depot',            'recurring_expense', 30, TRUE),
  ('rule_raleyss',         'description_contains', 'raley''s',       'expense', 'groceries', 'Raley''s',              'recurring_expense', 30, TRUE),
  ('rule_steam',           'description_contains', 'steamgames',     'expense', 'entertainment', 'Steam',             NULL, 30, TRUE)
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- 6. SEED default categories (two-tier)
-- ============================================================
INSERT INTO categories (id, name, parent_id, color, is_system, sort_order, exclude_from_spending)
VALUES
  -- Top level
  ('food',           'Food & Drink',       NULL, '#3fb950', TRUE, 1,  FALSE),
  ('transport',      'Transportation',     NULL, '#58a6ff', TRUE, 2,  FALSE),
  ('shopping',       'Shopping',           NULL, '#bc8cff', TRUE, 3,  FALSE),
  ('housing',        'Housing',            NULL, '#d29922', TRUE, 4,  FALSE),
  ('health',         'Health',             NULL, '#f85149', TRUE, 5,  FALSE),
  ('entertainment',  'Entertainment',      NULL, '#ff7b72', TRUE, 6,  FALSE),
  ('software',       'Software & Tech',    NULL, '#79c0ff', TRUE, 7,  FALSE),
  ('utilities',      'Utilities',          NULL, '#ffa657', TRUE, 8,  FALSE),
  ('personal',       'Personal',           NULL, '#cae8ff', TRUE, 9,  FALSE),
  ('pets',           'Pets',               NULL, '#7ee787', TRUE, 10, FALSE),
  -- Transfers / non-spending (excluded from spending totals)
  ('transfers',      'Transfers',          NULL, '#484f58', TRUE, 99, TRUE),
  ('investments',    'Investments',        NULL, '#3fb950', TRUE, 98, TRUE),
  ('debt',           'Debt Payments',      NULL, '#f85149', TRUE, 97, TRUE),

  -- Food children
  ('groceries',      'Groceries',          'food',          '#3fb950', TRUE, 1,  FALSE),
  ('dining',         'Dining Out',         'food',          '#3fb950', TRUE, 2,  FALSE),
  ('coffee',         'Coffee & Drinks',    'food',          '#3fb950', TRUE, 3,  FALSE),
  ('work_food',      'Work Food',          'food',          '#3fb950', TRUE, 4,  FALSE),

  -- Transport children
  ('fuel',           'Gas & Fuel',         'transport',     '#58a6ff', TRUE, 1,  FALSE),
  ('parking',        'Parking',            'transport',     '#58a6ff', TRUE, 2,  FALSE),
  ('rideshare',      'Rideshare',          'transport',     '#58a6ff', TRUE, 3,  FALSE),
  ('auto',           'Auto & DMV',         'transport',     '#58a6ff', TRUE, 4,  FALSE),

  -- Housing children
  ('rent',           'Rent / Mortgage',    'housing',       '#d29922', TRUE, 1,  FALSE),
  ('home',           'Home & Garden',      'housing',       '#d29922', TRUE, 2,  FALSE),
  ('insurance_home', 'Home Insurance',     'housing',       '#d29922', TRUE, 3,  FALSE),

  -- Entertainment children
  ('streaming',      'Streaming',          'entertainment', '#ff7b72', TRUE, 1,  FALSE),
  ('gaming',         'Gaming',             'entertainment', '#ff7b72', TRUE, 2,  FALSE),
  ('activities',     'Activities & Fun',   'entertainment', '#ff7b72', TRUE, 3,  FALSE),

  -- Health children
  ('medical',        'Medical',            'health',        '#f85149', TRUE, 1,  FALSE),
  ('pharmacy',       'Pharmacy',           'health',        '#f85149', TRUE, 2,  FALSE),
  ('fitness',        'Fitness',            'health',        '#f85149', TRUE, 3,  FALSE),
  ('insurance_health','Health Insurance',  'health',        '#f85149', TRUE, 4,  FALSE),
  ('vet',            'Vet & Pet Health',   'pets',          '#7ee787', TRUE, 1,  FALSE),
  ('pet_supplies',   'Pet Supplies',       'pets',          '#7ee787', TRUE, 2,  FALSE),

  -- Personal
  ('clothing',       'Clothing',           'personal',      '#cae8ff', TRUE, 1,  FALSE),
  ('personal_care',  'Personal Care',      'personal',      '#cae8ff', TRUE, 2,  FALSE),

  -- Investments children
  ('roth_ira',       'Roth IRA',           'investments',   '#3fb950', TRUE, 1,  TRUE),
  ('brokerage',      'Brokerage',          'investments',   '#3fb950', TRUE, 2,  TRUE)

ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- 7. UPDATE category references on merchant_rules
-- now that categories exist
-- ============================================================
UPDATE merchant_rules SET category_id = 'entertainment' WHERE category_id = 'software' AND id IN ('rule_claude_ai', 'rule_ovh');
UPDATE merchant_rules SET category_id = 'software'      WHERE id IN ('rule_claude_ai', 'rule_ovh');
UPDATE merchant_rules SET category_id = 'work_food'     WHERE id IN ('rule_market_work', 'rule_blend_catering', 'rule_tyg_xpress');
UPDATE merchant_rules SET category_id = 'streaming'     WHERE id IN ('rule_spotify', 'rule_disney_plus', 'rule_netflix_1', 'rule_roku_warner');

COMMIT;

\echo ''
\echo '✅ V2 Migration complete'
\echo ''
\echo 'Next step: run the classification backfill'
\echo 'docker compose exec backend python -m app.services.classify_backfill'
\echo ''
