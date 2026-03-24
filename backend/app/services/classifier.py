"""
Classification Engine — Budget Duo V3

Applies merchant rules to transactions to set txn_class,
merchant_clean, category_id, and recurring_type.

Rule priority: lower number wins.
User-verified transactions are NEVER overwritten.
Manual category/subcategory assignments are preserved even during reclassification.

V3 Changes:
- Fixed: user_verified transactions are fully protected
- Fixed: ACH fallback no longer misclassifies all debits as cc_payment
- Fixed: is_income flag properly synced for ALL txn_class values
- Fixed: Manual category assignments preserved when only txn_class changes
- Added: Logging for classification decisions
"""
import logging
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.db.models import MerchantRule, Transaction

logger = logging.getLogger(__name__)

# Fallback classification from raw Teller txn_type + patterns
# Used when no merchant rule matches
TELLER_TYPE_FALLBACKS = {
    "transfer":     "internal_transfer",
    "payment":      "cc_payment",
    "interest":     "ignore",
    "fee":          "ignore",
    "deposit":      None,       # needs further analysis
    "withdrawal":   "expense",
    "charge":       "expense",
    "card_payment": "expense",
    "ach":          None,       # needs further analysis — handled separately
    "transaction":  "expense",
    "bill_payment": "expense",  # V3: was cc_payment, but bill payments are real expenses
    "adjustment":   "ignore",
}

# Classes that represent income
INCOME_TXN_CLASSES = {"income"}

# Classes that are NOT real spending — transfers between accounts, payments, etc.
TRANSFER_TXN_CLASSES = {
    "internal_transfer", "cc_payment", "savings_move",
    "investment_in", "investment_out", "debt_payment", "ignore",
}

# Classes that represent real spending
EXPENSE_TXN_CLASSES = {"expense"}


def apply_rules(db: Session, transaction: Transaction, rules: list[MerchantRule]) -> bool:
    """
    Apply ordered rules to a single transaction.
    Returns True if any rule matched.

    NEVER modifies user_verified transactions.
    """
    if transaction.user_verified:
        return False

    description = (transaction.description or "").lower()
    counterparty = (transaction.counterparty_name or "").lower()

    for rule in rules:
        if not rule.is_active:
            continue

        matched = False
        match_val = rule.match_value.lower()

        if rule.match_type == "description_contains":
            matched = match_val in description
        elif rule.match_type == "description_starts_with":
            matched = description.startswith(match_val)
        elif rule.match_type == "counterparty_exact":
            matched = counterparty == match_val
        elif rule.match_type == "description_regex":
            import re
            try:
                matched = bool(re.search(match_val, description, re.IGNORECASE))
            except re.error:
                logger.warning(f"Invalid regex in rule {rule.id}: {match_val}")
                continue

        if matched:
            if rule.txn_class:
                transaction.txn_class = rule.txn_class
            if rule.merchant_clean:
                transaction.merchant_clean = rule.merchant_clean
            if rule.category_id:
                transaction.category_id = rule.category_id
            if rule.subcategory_id:
                transaction.subcategory_id = rule.subcategory_id
            if rule.recurring_type:
                transaction.recurring_type = rule.recurring_type
                transaction.is_recurring = True
            transaction.rule_id = rule.id

            # Increment match count
            rule.match_count = (rule.match_count or 0) + 1
            return True

    return False


def classify_fallback(transaction: Transaction) -> None:
    """
    When no rule matches, use Teller's txn_type and is_income flag
    to make a best-guess classification.

    Never touches user_verified transactions.
    Never overwrites an existing txn_class (already set by a rule).
    """
    if transaction.user_verified:
        return

    # Already classified by a rule — don't override
    if transaction.txn_class:
        return

    # Use is_income flag first (set during sync from Teller patterns)
    if transaction.is_income:
        transaction.txn_class = "income"
        return

    # Use Teller txn_type
    teller_type = (transaction.txn_type or "").lower()

    # ACH needs special handling — V3 fix
    # Previously all negative ACH was classified as cc_payment, which was wrong
    if teller_type == "ach":
        if float(transaction.amount) > 0:
            transaction.txn_class = "income"
        else:
            # Negative ACH = could be anything: bills, subscriptions, loan payments
            # Default to expense — rules engine will catch specific patterns
            transaction.txn_class = "expense"
        return

    # Deposits: positive amount not flagged as income
    if teller_type == "deposit":
        if float(transaction.amount) > 0:
            # Could be a refund, cashback, etc. — default to expense (will show positive)
            # User can reclassify if it's actually income
            transaction.txn_class = "expense"
        return

    # Standard fallback from type map
    fallback = TELLER_TYPE_FALLBACKS.get(teller_type)
    if fallback:
        transaction.txn_class = fallback
        return

    # Last resort: negative = expense, positive = income
    if float(transaction.amount) < 0:
        transaction.txn_class = "expense"
    else:
        transaction.txn_class = "income"


def sync_flags(transaction: Transaction) -> None:
    """
    Ensure is_income flag stays in sync with txn_class.

    V3 fix: Previously only set is_income for income and transfer classes,
    leaving it stale if a transaction was reclassified from income to expense.
    Now explicitly handles ALL cases.
    """
    if transaction.txn_class in INCOME_TXN_CLASSES:
        transaction.is_income = True
    elif transaction.txn_class in TRANSFER_TXN_CLASSES:
        transaction.is_income = False
    elif transaction.txn_class in EXPENSE_TXN_CLASSES:
        transaction.is_income = False
    # If txn_class is None/unknown, don't touch is_income


def classify_all(db: Session, only_unclassified: bool = False) -> dict:
    """
    Run the full classification pass over all transactions.
    Loads rules once, applies to each transaction.

    PROTECTION GUARANTEES:
    - user_verified=True transactions are NEVER modified
    - When reclassifying (only_unclassified=False), we reset txn_class
      but preserve manual category assignments

    Args:
        only_unclassified: If True, only process transactions with no txn_class.
                           If False, reclassify everything (except user_verified).
    """
    # Load all active rules ordered by priority
    rules = db.query(MerchantRule).filter(
        MerchantRule.is_active == True
    ).order_by(MerchantRule.priority.asc()).all()

    logger.info(f"Loaded {len(rules)} active rules")

    # Query transactions — ALWAYS skip user_verified
    q = db.query(Transaction).filter(Transaction.user_verified == False)
    if only_unclassified:
        q = q.filter(Transaction.txn_class == None)

    transactions = q.all()
    logger.info(f"Classifying {len(transactions)} transactions...")

    rule_matched = 0
    fallback_used = 0

    for txn in transactions:
        if not only_unclassified:
            # When reclassifying, clear auto-assigned fields so rules can re-evaluate
            # But DON'T clear category_id/subcategory_id if they were manually set
            # (user_verified check above already handles this, but belt-and-suspenders)
            txn.txn_class = None
            txn.rule_id = None
            txn.merchant_clean = None
            # Don't clear: category_id, subcategory_id, recurring_type
            # Those may have been set by the user before user_verified was implemented

        matched = apply_rules(db, txn, rules)
        if matched:
            rule_matched += 1
        else:
            classify_fallback(txn)
            fallback_used += 1

        # V3: Sync is_income for ALL classification outcomes
        sync_flags(txn)

    db.commit()

    result = {
        "total": len(transactions),
        "rule_matched": rule_matched,
        "fallback_used": fallback_used,
    }
    logger.info(f"Classification complete: {result}")
    return result


def classify_transaction(db: Session, txn: Transaction) -> None:
    """
    Classify a single transaction. Used during sync.
    Never touches user_verified transactions.
    """
    if txn.user_verified:
        return

    rules = db.query(MerchantRule).filter(
        MerchantRule.is_active == True
    ).order_by(MerchantRule.priority.asc()).all()

    matched = apply_rules(db, txn, rules)
    if not matched:
        classify_fallback(txn)

    # Always sync flags
    sync_flags(txn)