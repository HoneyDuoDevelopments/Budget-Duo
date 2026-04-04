"""
Classification Engine — Budget Duo V4.2

Standalone classifier that uses V4 category fields (cat_l1..l4).
Replaces the old classifier.py which referenced deprecated category_id/subcategory_id.

Used by:
- sync_service.py (Teller transaction sync)
- main.py _auto_classify (scraper imports, inline classification)

Rule priority: lower number wins.
User-verified transactions are NEVER overwritten.
"""
import logging
from sqlalchemy.orm import Session
from app.db.models import MerchantRule, Transaction

logger = logging.getLogger(__name__)

TXN_CLASS_TO_CAT_L1 = {
    "income":            "inc",
    "expense":           "exp",
    "subscription":      "sub",
    "savings_in":        "sav_in",
    "savings_out":       "sav_out",
    "investment_in":     "inv_in",
    "investment_out":    "inv_out",
    "cc_payment":        "cc_payment",
    "internal_transfer": "transfer",
    "ignore":            "ignore",
}


def rule_matches(rule: MerchantRule, t: Transaction) -> bool:
    """Check if a rule matches a transaction."""
    desc = (t.description or "").lower()
    val = rule.match_value.lower()

    if rule.match_type == "description_contains":
        return val in desc
    if rule.match_type == "description_starts_with":
        return desc.startswith(val)
    if rule.match_type == "counterparty_exact":
        return (t.counterparty_name or "").lower() == val
    if rule.match_type == "counterparty_contains":
        return val in (t.counterparty_name or "").lower()
    if rule.match_type == "description_regex":
        import re
        try:
            return bool(re.search(val, desc, re.IGNORECASE))
        except re.error:
            logger.warning(f"Invalid regex in rule {rule.id}: {val}")
            return False
    return False


def classify_transaction(db: Session, txn: Transaction) -> None:
    """
    Classify a single transaction using merchant rules + fallback logic.
    Uses V4 category fields (cat_l1..l4). Never touches user_verified transactions.

    Called during Teller sync for each new/updated transaction.
    """
    if txn.user_verified:
        return

    # Load all active rules ordered by priority
    rules = db.query(MerchantRule).filter(
        MerchantRule.is_active == True
    ).order_by(MerchantRule.priority.asc()).all()

    # Try to match a rule
    for rule in rules:
        if rule_matches(rule, txn):
            if rule.txn_class:
                txn.txn_class = rule.txn_class
                txn.cat_l1 = TXN_CLASS_TO_CAT_L1.get(rule.txn_class, txn.cat_l1)
            if rule.cat_l2:
                txn.cat_l2 = rule.cat_l2
            if rule.cat_l3:
                txn.cat_l3 = rule.cat_l3
            if rule.cat_l4:
                txn.cat_l4 = rule.cat_l4
            if rule.recurring_type:
                txn.recurring_type = rule.recurring_type
                txn.is_recurring = True
            if rule.merchant_clean:
                txn.merchant_clean = rule.merchant_clean
            txn.rule_id = rule.id
            rule.match_count = (rule.match_count or 0) + 1
            # Sync is_income flag
            _sync_income_flag(txn)
            return

    # No rule matched — use fallback logic
    _classify_fallback(txn)
    _sync_income_flag(txn)


def _classify_fallback(txn: Transaction) -> None:
    """
    When no rule matches, use account type, is_income flag, and amount sign
    to make a best-guess classification.
    """
    if txn.user_verified:
        return

    # Already classified by a rule — don't override
    if txn.txn_class:
        return

    # Use is_income flag first (set during sync from Teller patterns)
    if txn.is_income and float(txn.amount) > 0:
        txn.txn_class = "income"
        txn.cat_l1 = "inc"
        return

    # Check account type for context
    acct = txn.account
    if acct:
        # Savings accounts
        if acct.is_savings and not acct.exclude_from_savings:
            txn.txn_class = "savings_in" if float(txn.amount) > 0 else "savings_out"
            txn.cat_l1 = TXN_CLASS_TO_CAT_L1[txn.txn_class]
            return

        # Bills-only / exclude_from_savings accounts
        if acct.exclude_from_savings:
            txn.txn_class = "expense" if float(txn.amount) < 0 else "internal_transfer"
            txn.cat_l1 = TXN_CLASS_TO_CAT_L1[txn.txn_class]
            return

        # Credit cards
        if acct.type == "credit":
            txn.txn_class = "expense" if float(txn.amount) < 0 else "cc_payment"
            txn.cat_l1 = TXN_CLASS_TO_CAT_L1.get(txn.txn_class)
            return

    # Checking / unknown accounts — use Teller txn_type
    teller_type = (txn.txn_type or "").lower()

    # ACH needs special handling
    if teller_type == "ach":
        if float(txn.amount) > 0:
            txn.txn_class = "income"
            txn.cat_l1 = "inc"
        else:
            txn.txn_class = "expense"
            txn.cat_l1 = "exp"
        return

    # Standard fallback from type
    type_map = {
        "transfer":     "internal_transfer",
        "payment":      "cc_payment",
        "interest":     "ignore",
        "fee":          "ignore",
        "withdrawal":   "expense",
        "charge":       "expense",
        "card_payment": "expense",
        "transaction":  "expense",
        "bill_payment": "expense",
        "adjustment":   "ignore",
    }

    fallback = type_map.get(teller_type)
    if fallback:
        txn.txn_class = fallback
        txn.cat_l1 = TXN_CLASS_TO_CAT_L1.get(fallback)
        return

    # Deposit type
    if teller_type == "deposit":
        txn.txn_class = "expense" if float(txn.amount) < 0 else "income"
        txn.cat_l1 = TXN_CLASS_TO_CAT_L1.get(txn.txn_class)
        return

    # Last resort: sign-based
    if float(txn.amount) < 0:
        txn.txn_class = "expense"
        txn.cat_l1 = "exp"
    else:
        txn.txn_class = "internal_transfer"
        txn.cat_l1 = "transfer"


def _sync_income_flag(txn: Transaction) -> None:
    """Keep is_income flag in sync with txn_class."""
    if txn.txn_class == "income":
        txn.is_income = True
    elif txn.txn_class:
        txn.is_income = False