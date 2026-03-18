"""
Classification Engine — Budget Duo V2

Applies merchant rules to transactions to set txn_class,
merchant_clean, category_id, and recurring_type.

Rule priority: lower number wins.
User-verified transactions are never overwritten.
"""
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.db.models import MerchantRule, Transaction


# Fallback classification from raw Teller txn_type + patterns
# Used when no merchant rule matches
TELLER_TYPE_FALLBACKS = {
    "transfer": "internal_transfer",
    "payment":  "cc_payment",
    "interest": "ignore",
    "fee":      "ignore",
    "deposit":  None,   # needs further analysis
    "withdrawal": "expense",
    "charge":   "expense",
    "card_payment": "expense",
    "ach":      None,   # needs further analysis
    "transaction": "expense",
    "bill_payment": "cc_payment",
    "adjustment": "ignore",
}

INCOME_TXN_CLASSES = {"income"}
TRANSFER_TXN_CLASSES = {"internal_transfer", "cc_payment", "savings_move", "investment_in", "investment_out", "debt_payment", "ignore"}


def apply_rules(db: Session, transaction: Transaction, rules: list[MerchantRule]) -> bool:
    """
    Apply ordered rules to a single transaction.
    Returns True if any rule matched.
    Skips user_verified transactions.
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
            matched = counterparty == match_val.lower() or (transaction.counterparty_name or "").lower() == match_val.lower()
        elif rule.match_type == "description_regex":
            import re
            matched = bool(re.search(match_val, description, re.IGNORECASE))

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
    """
    if transaction.user_verified:
        return

    # Already classified by a rule
    if transaction.txn_class:
        return

    # Use is_income flag first
    if transaction.is_income:
        transaction.txn_class = "income"
        return

    # Use Teller txn_type
    teller_type = (transaction.txn_type or "").lower()
    fallback = TELLER_TYPE_FALLBACKS.get(teller_type)

    if fallback:
        transaction.txn_class = fallback
        return

    # ACH: positive = likely income or investment, negative = likely payment
    if teller_type == "ach":
        if float(transaction.amount) > 0:
            transaction.txn_class = "income"
        else:
            transaction.txn_class = "cc_payment"
        return

    # Deposit: positive + not income = investment_in or needs review
    if teller_type == "deposit":
        if float(transaction.amount) > 0:
            transaction.txn_class = "expense"  # Will be caught by user review
        return

    # Default: if it's a debit, it's an expense
    if float(transaction.amount) < 0:
        transaction.txn_class = "expense"
    else:
        transaction.txn_class = "income"


def classify_all(db: Session, only_unclassified: bool = False) -> dict:
    """
    Run the full classification pass over all transactions.
    Loads rules once, applies to each transaction.

    Args:
        only_unclassified: If True, only process transactions with no txn_class.
                           If False, reclassify everything (except user_verified).
    """
    # Load all active rules ordered by priority
    rules = db.query(MerchantRule).filter(
        MerchantRule.is_active == True
    ).order_by(MerchantRule.priority.asc()).all()

    print(f"Loaded {len(rules)} active rules")

    # Query transactions
    q = db.query(Transaction).filter(Transaction.user_verified == False)
    if only_unclassified:
        q = q.filter(Transaction.txn_class == None)

    transactions = q.all()
    print(f"Classifying {len(transactions)} transactions...")

    rule_matched = 0
    fallback_used = 0

    for txn in transactions:
        matched = apply_rules(db, txn, rules)
        if matched:
            rule_matched += 1
        else:
            classify_fallback(txn)
            fallback_used += 1

        # Sync is_income with txn_class
        if txn.txn_class == "income":
            txn.is_income = True
        elif txn.txn_class in TRANSFER_TXN_CLASSES:
            txn.is_income = False

    db.commit()

    result = {
        "total": len(transactions),
        "rule_matched": rule_matched,
        "fallback_used": fallback_used,
    }
    print(f"Classification complete: {result}")
    return result


def classify_transaction(db: Session, txn: Transaction) -> None:
    """Classify a single transaction. Used during sync."""
    rules = db.query(MerchantRule).filter(
        MerchantRule.is_active == True
    ).order_by(MerchantRule.priority.asc()).all()

    matched = apply_rules(db, txn, rules)
    if not matched:
        classify_fallback(txn)
