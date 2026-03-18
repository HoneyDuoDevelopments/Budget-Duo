import uuid
import json
from datetime import date, timedelta
from decimal import Decimal
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.db.models import Account, Transaction, SyncLog, Enrollment, AccountBalance
from app.services.teller_client import TellerClient
from app.services.classifier import classify_transaction
from app.config import settings


INCOME_PATTERNS = [
    "payroll", "direct dep", "direct deposit",
    "ach credit", "gusto", "adp", "paychex", "intuit payroll",
]

INCOME_COUNTERPARTIES = ["tesla", "tesla motors"]


def is_income_transaction(t: dict, amount: Decimal) -> bool:
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


def upsert_transactions(db: Session, account_id: str, raw_txns: list) -> tuple[int, int]:
    added = updated = 0
    for t in raw_txns:
        txn_id = t["id"]
        amount = Decimal(str(t["amount"]))
        details = t.get("details") or {}
        category = details.get("category")
        counterparty = details.get("counterparty") or {}
        income = is_income_transaction(t, amount)

        existing = db.get(Transaction, txn_id)
        if existing:
            # Only update fields that don't require user re-verification
            existing.amount = amount
            existing.date = date.fromisoformat(t["date"])
            existing.status = t["status"]
            existing.teller_category = category
            existing.counterparty_name = counterparty.get("name")
            existing.counterparty_type = counterparty.get("type")
            existing.is_income = income
            existing.raw_json = json.dumps(t)
            # Re-classify only if not user-verified
            if not existing.user_verified:
                classify_transaction(db, existing)
            updated += 1
        else:
            txn = Transaction(
                id=txn_id,
                account_id=account_id,
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


def sync_account(db: Session, account: Account, enrollment: Enrollment) -> dict:
    if account.is_bills_only:
        return {"skipped": True, "reason": "bills_only"}

    client = TellerClient(enrollment.access_token)
    from_date = (date.today() - timedelta(days=10)).isoformat()

    try:
        txns = client.get_transactions(account.id, from_date=from_date)
    except Exception as e:
        log = SyncLog(
            id=str(uuid.uuid4()),
            account_id=account.id,
            sync_type="scheduled",
            status="failed",
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
        print(f"Balance fetch failed for {account.name}: {e}")

    log = SyncLog(
        id=str(uuid.uuid4()),
        account_id=account.id,
        sync_type="scheduled",
        status="success",
        txns_added=str(added),
        txns_updated=str(updated),
    )
    db.add(log)
    db.commit()

    return {"added": added, "updated": updated}


def sync_all(db: Session) -> dict:
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
        print(f"Synced {account.name}: {result}")

    return results


def backfill_income(db: Session):
    """Legacy backfill - kept for compatibility"""
    print("Running income backfill...")
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
    print(f"Income backfill complete — flagged {fixed} transactions")
    return fixed
