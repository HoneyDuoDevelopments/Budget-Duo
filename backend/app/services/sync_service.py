"""
Sync Service — Budget Duo V3

Pulls transactions from Teller API and upserts them into the database.
Runs classification on new/updated transactions.

V3 Changes:
- Fixed: Account alias resolution during sync
- Fixed: Configurable sync window (default 30 days, initial backfill 90 days)
- Fixed: sync_log txns_added/updated as integers
- Improved: Better error handling and logging
"""
import uuid
import json
import logging
from datetime import date, timedelta
from decimal import Decimal
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.db.models import Account, Transaction, SyncLog, Enrollment, AccountBalance
from app.services.teller_client import TellerClient
from app.services.classifier import classify_transaction
from app.config import settings

logger = logging.getLogger(__name__)

# Sync window configuration
DEFAULT_SYNC_DAYS = 30      # Regular sync looks back 30 days
INITIAL_SYNC_DAYS = 90      # First sync for an account looks back 90 days

INCOME_PATTERNS = [
    "payroll", "direct dep", "direct deposit",
    "ach credit", "gusto", "adp", "paychex", "intuit payroll",
]

INCOME_COUNTERPARTIES = ["tesla", "tesla motors"]


def is_income_transaction(t: dict, amount: Decimal) -> bool:
    """Detect income from Teller transaction data."""
    if amount <= 0:
        return False
    description = (t.get("description") or "").lower()
    counterparty = ((t.get("details") or {}).get("counterparty") or {})
    counterparty_name = (counterparty.get("name") or "").lower()
    teller_category = ((t.get("details") or {}).get("category") or "").lower()

    if teller_category == "income":
        return True
    for cp in INCOME_COUNTERPARTIES:
        if cp in counterparty_name:
            return True
    for pattern in INCOME_PATTERNS:
        if pattern in description:
            return True
    return False


def resolve_account_id(account_id: str) -> str:
    """
    Resolve alias account IDs to their canonical ID.

    V3 fix: Teller may return transactions under alias account IDs
    (e.g., when the same account appears under different enrollment contexts).
    We need to map these back to the canonical account ID stored in our DB.
    """
    return settings.ACCOUNT_ALIASES.get(account_id, account_id)


def upsert_transactions(db: Session, account_id: str, raw_txns: list) -> tuple[int, int]:
    """
    Insert new transactions or update existing ones.
    Runs classification on new/changed transactions.
    """
    added = updated = 0

    for t in raw_txns:
        txn_id = t["id"]
        amount = Decimal(str(t["amount"]))
        details = t.get("details") or {}
        category = details.get("category")
        counterparty = details.get("counterparty") or {}
        income = is_income_transaction(t, amount)

        # V3: Resolve alias account IDs
        resolved_account_id = resolve_account_id(t.get("account_id", account_id))
        # Use the account_id passed to us (already canonical) unless the
        # transaction itself references a different account
        effective_account_id = resolved_account_id if t.get("account_id") else account_id

        existing = db.get(Transaction, txn_id)
        if existing:
            # Update mutable fields — never touch user-verified classification
            existing.amount = amount
            existing.date = date.fromisoformat(t["date"])
            existing.status = t["status"]
            existing.teller_category = category
            existing.counterparty_name = counterparty.get("name")
            existing.counterparty_type = counterparty.get("type")
            existing.is_income = income
            existing.raw_json = json.dumps(t)

            # Re-classify ONLY if not user-verified
            if not existing.user_verified:
                classify_transaction(db, existing)

            updated += 1
        else:
            txn = Transaction(
                id=txn_id,
                account_id=effective_account_id,
                amount=amount,
                date=date.fromisoformat(t["date"]),
                description=t.get("description", ""),
                counterparty_name=counterparty.get("name"),
                counterparty_type=counterparty.get("type"),
                teller_category=category,
                txn_type=t.get("type"),
                status=t["status"],
                is_income=income,
                raw_json=json.dumps(t),
            )
            db.add(txn)
            # Flush so the txn has an ID before classification
            db.flush()
            classify_transaction(db, txn)
            added += 1

    return added, updated


def get_sync_window(db: Session, account_id: str) -> str:
    """
    Determine how far back to sync.

    V3: Uses a longer window for first-time syncs to catch historical transactions.
    Regular syncs use 30 days to catch late-posting credit card transactions.
    """
    # Check if we've ever synced this account
    has_synced = db.execute(text(
        "SELECT 1 FROM sync_log WHERE account_id = :aid AND status = 'success' LIMIT 1"
    ), {"aid": account_id}).fetchone()

    if has_synced:
        days = DEFAULT_SYNC_DAYS
    else:
        days = INITIAL_SYNC_DAYS
        logger.info(f"First sync for account {account_id} — using {days}-day backfill")

    return (date.today() - timedelta(days=days)).isoformat()


def sync_account(db: Session, account: Account, enrollment: Enrollment) -> dict:
    """Sync a single account's transactions and balance."""
    if account.is_bills_only:
        return {"skipped": True, "reason": "bills_only"}

    client = TellerClient(enrollment.access_token)
    from_date = get_sync_window(db, account.id)

    try:
        txns = client.get_transactions(account.id, from_date=from_date)
    except Exception as e:
        logger.error(f"Sync failed for {account.name}: {e}")
        log = SyncLog(
            id=str(uuid.uuid4()),
            account_id=account.id,
            sync_type="scheduled",
            status="failed",
            txns_added=0,
            txns_updated=0,
            error_message=str(e),
        )
        db.add(log)
        db.commit()
        return {"error": str(e)}

    added, updated = upsert_transactions(db, account.id, txns)

    # Update balance cache
    try:
        bal = client.get_balance(account.id)
        existing_bal = db.get(AccountBalance, account.id)
        if existing_bal:
            existing_bal.ledger = Decimal(str(bal.get("ledger", 0) or 0))
            existing_bal.available = Decimal(str(bal.get("available", 0) or 0))
            from sqlalchemy.sql import func
            existing_bal.fetched_at = func.now()
        else:
            db.add(AccountBalance(
                account_id=account.id,
                ledger=Decimal(str(bal.get("ledger", 0) or 0)),
                available=Decimal(str(bal.get("available", 0) or 0)),
            ))
    except Exception as e:
        logger.warning(f"Balance fetch failed for {account.name}: {e}")

    log = SyncLog(
        id=str(uuid.uuid4()),
        account_id=account.id,
        sync_type="scheduled",
        status="success",
        txns_added=added,
        txns_updated=updated,
    )
    db.add(log)
    db.commit()

    return {"added": added, "updated": updated}


def sync_all(db: Session) -> dict:
    """Sync all active, non-bills-only accounts."""
    accounts = db.query(Account).filter(
        Account.status == "open",
        Account.is_bills_only == False,
    ).all()

    results = {}
    for account in accounts:
        enrollment = db.get(Enrollment, account.enrollment_id)
        if not enrollment or enrollment.status != "active":
            continue
        result = sync_account(db, account, enrollment)
        results[account.name] = result
        logger.info(f"Synced {account.name}: {result}")

    return results


def backfill_income(db: Session):
    """Legacy backfill — kept for compatibility."""
    logger.info("Running income backfill...")
    txns = db.execute(text(
        "SELECT id, raw_json FROM transactions WHERE raw_json IS NOT NULL"
    )).fetchall()
    fixed = 0
    for row in txns:
        try:
            t = json.loads(row[1])
            amount = Decimal(str(t["amount"]))
            income = is_income_transaction(t, amount)
            if income:
                db.execute(text(
                    "UPDATE transactions SET is_income = true WHERE id = :id"
                ), {"id": row[0]})
                fixed += 1
        except Exception:
            continue
    db.commit()
    logger.info(f"Income backfill complete — flagged {fixed} transactions")
    return fixed