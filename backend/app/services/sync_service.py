import uuid
import json
from datetime import date, timedelta
from decimal import Decimal
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.db.models import Account, Transaction, SyncLog, Enrollment
from app.services.teller_client import TellerClient
from app.config import settings

# Income detection patterns — description-based since Teller enrichment is unreliable
INCOME_PATTERNS = [
    "payroll",
    "direct dep",
    "direct deposit",
    "ach credit",
    "gusto",
    "adp",
    "paychex",
    "intuit payroll",
]

# These counterparties are always income regardless of category
INCOME_COUNTERPARTIES = [
    "tesla",
    "tesla motors",
]


def is_income_transaction(t: dict, amount: Decimal) -> bool:
    """Detect income by description patterns and amount sign."""
    # Must be a positive amount (credit to account)
    if amount <= 0:
        return False

    description = (t.get("description") or "").lower()
    counterparty = ((t.get("details") or {}).get("counterparty") or {})
    counterparty_name = (counterparty.get("name") or "").lower()
    teller_category = ((t.get("details") or {}).get("category") or "").lower()

    # Teller says income
    if teller_category == "income":
        return True

    # Known income counterparties
    for cp in INCOME_COUNTERPARTIES:
        if cp in counterparty_name:
            return True

    # Description patterns
    for pattern in INCOME_PATTERNS:
        if pattern in description:
            return True

    # Tesla payroll specific pattern
    if "tesla motors" in description and "payroll" in description:
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
            existing.amount = amount
            existing.date = date.fromisoformat(t["date"])
            existing.status = t["status"]
            existing.teller_category = category
            existing.counterparty_name = counterparty.get("name")
            existing.counterparty_type = counterparty.get("type")
            existing.is_income = income
            existing.raw_json = json.dumps(t)
            updated += 1
        else:
            db.add(Transaction(
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
            ))
            added += 1
    return added, updated


def backfill_income(db: Session):
    """Fix income detection on existing transactions using raw_json."""
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


def sync_all(db: Session):
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

    detect_recurring(db)
    return results


def detect_recurring(db: Session):
    results = db.execute(text("""
        SELECT counterparty_name, COUNT(*) as count,
               STDDEV(ABS(amount)) as stddev_amount
        FROM transactions
        WHERE counterparty_name IS NOT NULL
          AND status = 'posted'
          AND is_income = false
        GROUP BY counterparty_name
        HAVING COUNT(*) >= 3
          AND (STDDEV(ABS(amount)) < 5.00 OR STDDEV(ABS(amount)) IS NULL)
        ORDER BY count DESC
    """)).fetchall()

    recurring_names = [row[0] for row in results]

    if recurring_names:
        db.execute(text("""
            UPDATE transactions
            SET is_recurring = true,
                recurring_group = counterparty_name
            WHERE counterparty_name = ANY(:names)
              AND is_income = false
        """), {"names": recurring_names})
        db.commit()
        print(f"Flagged recurring for {len(recurring_names)} merchants")
