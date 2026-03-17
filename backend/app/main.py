from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler
from app.db.models import Base
from app.db.session import engine, SessionLocal
from app.services.sync_service import sync_all

# Create all tables on startup
Base.metadata.create_all(bind=engine)

app = FastAPI(title="Budget Duo API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/accounts")
def get_accounts():
    db = SessionLocal()
    try:
        from app.db.models import Account
        accounts = db.query(Account).filter(Account.status == "open").all()
        return [
            {
                "id": a.id,
                "name": a.name,
                "institution": a.institution_name,
                "type": a.type,
                "subtype": a.subtype,
                "last_four": a.last_four,
                "owner": a.owner,
                "role": a.role,
                "is_bills_only": a.is_bills_only,
                "is_savings": a.is_savings,
            }
            for a in accounts
        ]
    finally:
        db.close()


@app.get("/api/transactions")
def get_transactions(account_id: str = None, limit: int = 100, offset: int = 0):
    db = SessionLocal()
    try:
        from app.db.models import Transaction
        q = db.query(Transaction)
        if account_id:
            q = q.filter(Transaction.account_id == account_id)
        q = q.order_by(Transaction.date.desc()).offset(offset).limit(limit)
        txns = q.all()
        return [
            {
                "id": t.id,
                "account_id": t.account_id,
                "amount": float(t.amount),
                "date": t.date.isoformat(),
                "description": t.description,
                "counterparty": t.counterparty_name,
                "teller_category": t.teller_category,
                "custom_category": t.custom_category,
                "status": t.status,
                "is_income": t.is_income,
                "is_recurring": t.is_recurring,
                "recurring_group": t.recurring_group,
                "type": t.txn_type,
            }
            for t in txns
        ]
    finally:
        db.close()


@app.patch("/api/transactions/{txn_id}/category")
def set_category(txn_id: str, body: dict):
    db = SessionLocal()
    try:
        from app.db.models import Transaction
        txn = db.get(Transaction, txn_id)
        if not txn:
            return {"error": "not found"}, 404
        txn.custom_category = body.get("category")
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@app.post("/api/sync")
def manual_sync():
    db = SessionLocal()
    try:
        results = sync_all(db)
        return {"ok": True, "results": results}
    finally:
        db.close()

@app.post("/api/backfill-income")
def backfill_income_endpoint():
    from app.services.sync_service import backfill_income
    db = SessionLocal()
    try:
        fixed = backfill_income(db)
        return {"ok": True, "fixed": fixed}
    finally:
        db.close()


# Scheduled daily sync at 6am
scheduler = BackgroundScheduler()

def scheduled_sync():
    db = SessionLocal()
    try:
        sync_all(db)
    finally:
        db.close()

scheduler.add_job(scheduled_sync, "cron", hour=6, minute=0)
scheduler.start()