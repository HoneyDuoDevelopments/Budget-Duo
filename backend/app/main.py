from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from apscheduler.schedulers.background import BackgroundScheduler
from typing import Optional
from decimal import Decimal
import os

from app.db.models import Base, Account, Transaction, Category, MerchantRule, AccountBalance
from app.db.session import engine, SessionLocal
from app.services.sync_service import sync_all, backfill_income
from app.services.classifier import classify_all, classify_transaction

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Budget Duo API v2")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_PATH = "/app/static/index.html"


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
def health():
    return {"status": "ok", "version": "2.0"}


# ============================================================
# ACCOUNTS
# ============================================================

@app.get("/api/accounts")
def get_accounts():
    db = SessionLocal()
    try:
        accounts = db.query(Account).filter(Account.status == "open").all()
        result = []
        for a in accounts:
            bal = a.balance
            result.append({
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
                "balance_ledger": float(bal.ledger) if bal and bal.ledger else None,
                "balance_available": float(bal.available) if bal and bal.available else None,
                "balance_fetched_at": bal.fetched_at.isoformat() if bal and bal.fetched_at else None,
            })
        return result
    finally:
        db.close()


# ============================================================
# TRANSACTIONS
# ============================================================

@app.get("/api/transactions")
def get_transactions(
    account_id: Optional[str] = None,
    owner: Optional[str] = None,
    txn_class: Optional[str] = None,
    category_id: Optional[str] = None,
    recurring_type: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = Query(default=500, le=2000),
    offset: int = 0,
):
    db = SessionLocal()
    try:
        q = db.query(Transaction)
        if account_id:
            q = q.filter(Transaction.account_id == account_id)
        if txn_class:
            q = q.filter(Transaction.txn_class == txn_class)
        if category_id:
            q = q.filter(
                (Transaction.category_id == category_id) |
                (Transaction.subcategory_id == category_id)
            )
        if recurring_type:
            q = q.filter(Transaction.recurring_type == recurring_type)
        if search:
            s = f"%{search.lower()}%"
            from sqlalchemy import or_, func
            q = q.filter(or_(
                func.lower(Transaction.description).like(s),
                func.lower(Transaction.counterparty_name).like(s),
                func.lower(Transaction.merchant_clean).like(s),
            ))
        if owner:
            q = q.join(Account).filter(Account.owner == owner)

        q = q.order_by(Transaction.date.desc()).offset(offset).limit(limit)
        txns = q.all()

        return [_serialize_txn(t) for t in txns]
    finally:
        db.close()


def _serialize_txn(t: Transaction) -> dict:
    return {
        "id": t.id,
        "account_id": t.account_id,
        "amount": float(t.amount),
        "date": t.date.isoformat(),
        "description": t.description,
        "counterparty": t.counterparty_name,
        "merchant_clean": t.merchant_clean,
        "teller_category": t.teller_category,
        "txn_class": t.txn_class,
        "category_id": t.category_id,
        "subcategory_id": t.subcategory_id,
        "custom_category": t.custom_category,  # legacy
        "recurring_type": t.recurring_type,
        "status": t.status,
        "is_income": t.is_income,
        "is_recurring": t.is_recurring,
        "user_verified": t.user_verified,
        "rule_id": t.rule_id,
        "type": t.txn_type,
    }


@app.patch("/api/transactions/{txn_id}")
def update_transaction(txn_id: str, body: dict):
    """
    Update classification, category, or recurring type on a transaction.
    Always marks as user_verified=True so auto-classifier won't overwrite.
    """
    db = SessionLocal()
    try:
        txn = db.get(Transaction, txn_id)
        if not txn:
            raise HTTPException(status_code=404, detail="Transaction not found")

        if "txn_class" in body:
            txn.txn_class = body["txn_class"]
        if "category_id" in body:
            txn.category_id = body["category_id"] or None
            txn.custom_category = body["category_id"] or None  # keep legacy in sync
        if "subcategory_id" in body:
            txn.subcategory_id = body["subcategory_id"] or None
        if "recurring_type" in body:
            txn.recurring_type = body["recurring_type"] or None
            txn.is_recurring = bool(body["recurring_type"])
        if "merchant_clean" in body:
            txn.merchant_clean = body["merchant_clean"] or None

        txn.user_verified = True
        db.commit()
        return _serialize_txn(txn)
    finally:
        db.close()


# Legacy endpoint — kept for backwards compat with v1 frontend
@app.patch("/api/transactions/{txn_id}/category")
def set_category_legacy(txn_id: str, body: dict):
    db = SessionLocal()
    try:
        txn = db.get(Transaction, txn_id)
        if not txn:
            raise HTTPException(status_code=404, detail="not found")
        cat = body.get("category")
        txn.custom_category = cat or None
        txn.category_id = cat or None
        txn.user_verified = True
        db.commit()
        return {"ok": True}
    finally:
        db.close()


# ============================================================
# CATEGORIES
# ============================================================

@app.get("/api/categories")
def get_categories():
    db = SessionLocal()
    try:
        cats = db.query(Category).order_by(
            Category.sort_order.asc(), Category.name.asc()
        ).all()
        return [_serialize_category(c) for c in cats]
    finally:
        db.close()


def _serialize_category(c: Category) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "parent_id": c.parent_id,
        "color": c.color,
        "icon": c.icon,
        "is_system": c.is_system,
        "budget_amount": float(c.budget_amount) if c.budget_amount else None,
        "budget_period": c.budget_period,
        "sort_order": c.sort_order,
        "exclude_from_spending": c.exclude_from_spending,
    }


@app.post("/api/categories")
def create_category(body: dict):
    db = SessionLocal()
    try:
        import re
        name = (body.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Name required")

        # Generate slug from name
        slug = re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')
        # Ensure uniqueness
        base_slug = slug
        i = 2
        while db.get(Category, slug):
            slug = f"{base_slug}_{i}"
            i += 1

        parent_id = body.get("parent_id") or None
        if parent_id and not db.get(Category, parent_id):
            raise HTTPException(status_code=400, detail="Parent category not found")

        cat = Category(
            id=slug,
            name=name,
            parent_id=parent_id,
            color=body.get("color", "#58a6ff"),
            icon=body.get("icon"),
            is_system=False,
            budget_amount=body.get("budget_amount"),
            sort_order=body.get("sort_order", 50),
            exclude_from_spending=body.get("exclude_from_spending", False),
        )
        db.add(cat)
        db.commit()
        return _serialize_category(cat)
    finally:
        db.close()


@app.patch("/api/categories/{cat_id}")
def update_category(cat_id: str, body: dict):
    db = SessionLocal()
    try:
        cat = db.get(Category, cat_id)
        if not cat:
            raise HTTPException(status_code=404, detail="Category not found")

        if "name" in body:
            cat.name = body["name"]
        if "color" in body:
            cat.color = body["color"]
        if "icon" in body:
            cat.icon = body["icon"]
        if "budget_amount" in body:
            cat.budget_amount = body["budget_amount"]
        if "parent_id" in body:
            cat.parent_id = body["parent_id"] or None
        if "exclude_from_spending" in body:
            cat.exclude_from_spending = body["exclude_from_spending"]

        db.commit()
        return _serialize_category(cat)
    finally:
        db.close()


@app.delete("/api/categories/{cat_id}")
def delete_category(cat_id: str):
    db = SessionLocal()
    try:
        cat = db.get(Category, cat_id)
        if not cat:
            raise HTTPException(status_code=404, detail="Category not found")
        if cat.is_system:
            raise HTTPException(status_code=400, detail="Cannot delete system categories")

        # Check for children
        children = db.query(Category).filter(Category.parent_id == cat_id).count()
        if children > 0:
            raise HTTPException(status_code=400, detail=f"Category has {children} subcategories. Delete or reassign them first.")

        # Unassign from transactions
        db.query(Transaction).filter(Transaction.category_id == cat_id).update({
            "category_id": None, "custom_category": None, "user_verified": False
        })
        db.query(Transaction).filter(Transaction.subcategory_id == cat_id).update({
            "subcategory_id": None
        })

        db.delete(cat)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


# ============================================================
# MERCHANT RULES
# ============================================================

@app.get("/api/rules")
def get_rules():
    db = SessionLocal()
    try:
        rules = db.query(MerchantRule).order_by(
            MerchantRule.priority.asc(), MerchantRule.match_count.desc()
        ).all()
        return [_serialize_rule(r) for r in rules]
    finally:
        db.close()


def _serialize_rule(r: MerchantRule) -> dict:
    return {
        "id": r.id,
        "match_type": r.match_type,
        "match_value": r.match_value,
        "txn_class": r.txn_class,
        "category_id": r.category_id,
        "subcategory_id": r.subcategory_id,
        "recurring_type": r.recurring_type,
        "merchant_clean": r.merchant_clean,
        "priority": r.priority,
        "is_system": r.is_system,
        "is_active": r.is_active,
        "match_count": r.match_count,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@app.post("/api/rules")
def create_rule(body: dict):
    """Create a user merchant rule and optionally apply it immediately."""
    db = SessionLocal()
    try:
        import uuid as _uuid
        import re

        match_value = (body.get("match_value") or "").strip()
        if not match_value:
            raise HTTPException(status_code=400, detail="match_value required")

        slug = "rule_user_" + re.sub(r'[^a-z0-9]+', '_', match_value.lower())[:40]
        base = slug
        i = 2
        while db.get(MerchantRule, slug):
            slug = f"{base}_{i}"
            i += 1

        rule = MerchantRule(
            id=slug,
            match_type=body.get("match_type", "description_contains"),
            match_value=match_value,
            txn_class=body.get("txn_class"),
            category_id=body.get("category_id"),
            subcategory_id=body.get("subcategory_id"),
            recurring_type=body.get("recurring_type"),
            merchant_clean=body.get("merchant_clean"),
            priority=body.get("priority", 100),
            is_system=False,
            is_active=True,
        )
        db.add(rule)
        db.commit()

        # Optionally apply to existing transactions
        if body.get("apply_to_existing", False):
            classify_all(db, only_unclassified=False)

        return _serialize_rule(rule)
    finally:
        db.close()


@app.patch("/api/rules/{rule_id}")
def update_rule(rule_id: str, body: dict):
    db = SessionLocal()
    try:
        rule = db.get(MerchantRule, rule_id)
        if not rule:
            raise HTTPException(status_code=404, detail="Rule not found")

        for field in ["match_type", "match_value", "txn_class", "category_id",
                      "subcategory_id", "recurring_type", "merchant_clean",
                      "priority", "is_active"]:
            if field in body:
                setattr(rule, field, body[field])

        db.commit()

        if body.get("apply_to_existing", False):
            classify_all(db, only_unclassified=False)

        return _serialize_rule(rule)
    finally:
        db.close()


@app.delete("/api/rules/{rule_id}")
def delete_rule(rule_id: str):
    db = SessionLocal()
    try:
        rule = db.get(MerchantRule, rule_id)
        if not rule:
            raise HTTPException(status_code=404, detail="Rule not found")
        if rule.is_system:
            raise HTTPException(status_code=400, detail="Cannot delete system rules. Disable them instead.")
        db.delete(rule)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


# ============================================================
# SUMMARY / DASHBOARD
# ============================================================

@app.get("/api/summary")
def get_summary():
    db = SessionLocal()
    try:
        from sqlalchemy import text
        from datetime import date

        today = date.today()
        month_start = today.replace(day=1).isoformat()

        # Real spending MTD — only expense class
        spending_row = db.execute(text("""
            SELECT COALESCE(SUM(ABS(amount)), 0)
            FROM transactions
            WHERE txn_class = 'expense'
              AND amount < 0
              AND date >= :month_start
              AND status != 'pending'
        """), {"month_start": month_start}).fetchone()

        # Real income MTD
        income_row = db.execute(text("""
            SELECT COALESCE(SUM(amount), 0)
            FROM transactions
            WHERE txn_class = 'income'
              AND amount > 0
              AND date >= :month_start
              AND status != 'pending'
        """), {"month_start": month_start}).fetchone()

        # Subscriptions monthly total
        sub_row = db.execute(text("""
            SELECT COALESCE(SUM(ABS(amount)), 0), COUNT(DISTINCT merchant_clean)
            FROM transactions
            WHERE recurring_type = 'subscription'
              AND txn_class = 'expense'
              AND date >= :month_start
        """), {"month_start": month_start}).fetchone()

        # CC payments MTD
        cc_row = db.execute(text("""
            SELECT COALESCE(SUM(ABS(amount)), 0)
            FROM transactions
            WHERE txn_class = 'cc_payment'
              AND date >= :month_start
        """), {"month_start": month_start}).fetchone()

        # Account balances
        balances = db.execute(text("""
            SELECT a.type, a.is_savings, COALESCE(ab.ledger, 0) as ledger
            FROM accounts a
            LEFT JOIN account_balances ab ON ab.account_id = a.id
            WHERE a.status = 'open'
        """)).fetchall()

        cash_on_hand = sum(float(r[2]) for r in balances if r[0] == 'depository' and not r[1])
        savings_total = sum(float(r[2]) for r in balances if r[1])
        # Credit cards: ledger is what you owe (positive = balance owed)
        credit_debt = sum(abs(float(r[2])) for r in balances if r[0] == 'credit')

        # Uncategorized count
        uncat_row = db.execute(text("""
            SELECT COUNT(*)
            FROM transactions
            WHERE txn_class = 'expense'
              AND category_id IS NULL
              AND user_verified = FALSE
              AND status = 'posted'
              AND date >= :month_start
        """), {"month_start": month_start}).fetchone()

        return {
            "spending_mtd": float(spending_row[0]),
            "income_mtd": float(income_row[0]),
            "subscriptions_mtd": float(sub_row[0]),
            "subscription_count": int(sub_row[1]),
            "cc_paid_mtd": float(cc_row[0]),
            "cash_on_hand": cash_on_hand,
            "savings_total": savings_total,
            "credit_debt": credit_debt,
            "uncategorized_count": int(uncat_row[0]),
            "month_start": month_start,
        }
    finally:
        db.close()


@app.get("/api/summary/by-category")
def get_spending_by_category():
    """Spending breakdown by category for current month."""
    db = SessionLocal()
    try:
        from sqlalchemy import text
        from datetime import date

        month_start = date.today().replace(day=1).isoformat()

        rows = db.execute(text("""
            SELECT
              COALESCE(t.category_id, 'uncategorized') as cat_id,
              COALESCE(c.name, 'Uncategorized') as cat_name,
              COALESCE(c.color, '#484f58') as color,
              COALESCE(cp.name, c.name, 'Uncategorized') as parent_name,
              COUNT(*) as txn_count,
              SUM(ABS(t.amount))::numeric(12,2) as total
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id
            LEFT JOIN categories cp ON cp.id = c.parent_id
            WHERE t.txn_class = 'expense'
              AND t.amount < 0
              AND t.date >= :month_start
              AND t.status != 'pending'
            GROUP BY COALESCE(t.category_id, 'uncategorized'), c.name, c.color, cp.name
            ORDER BY total DESC
        """), {"month_start": month_start}).fetchall()

        return [
            {
                "category_id": r[0],
                "category_name": r[1],
                "color": r[2],
                "parent_name": r[3],
                "txn_count": r[4],
                "total": float(r[5]),
            }
            for r in rows
        ]
    finally:
        db.close()


@app.get("/api/summary/monthly")
def get_monthly_summary(months: int = 6):
    """Month-over-month spending vs income."""
    db = SessionLocal()
    try:
        from sqlalchemy import text
        rows = db.execute(text("""
            SELECT
              TO_CHAR(DATE_TRUNC('month', date), 'Mon YYYY') as month,
              DATE_TRUNC('month', date) as month_date,
              SUM(CASE WHEN txn_class = 'income' AND amount > 0 THEN amount ELSE 0 END)::numeric(12,2) as income,
              SUM(CASE WHEN txn_class = 'expense' AND amount < 0 THEN ABS(amount) ELSE 0 END)::numeric(12,2) as spending,
              SUM(CASE WHEN txn_class = 'investment_out' THEN ABS(amount) ELSE 0 END)::numeric(12,2) as investments,
              SUM(CASE WHEN txn_class = 'cc_payment' THEN ABS(amount) ELSE 0 END)::numeric(12,2) as cc_paid,
              COUNT(CASE WHEN txn_class = 'expense' THEN 1 END) as expense_count
            FROM transactions
            WHERE status != 'pending'
            GROUP BY DATE_TRUNC('month', date)
            ORDER BY DATE_TRUNC('month', date) DESC
            LIMIT :months
        """), {"months": months}).fetchall()

        return [
            {
                "month": r[0],
                "income": float(r[2]),
                "spending": float(r[3]),
                "investments": float(r[4]),
                "cc_paid": float(r[5]),
                "expense_count": r[6],
            }
            for r in rows
        ]
    finally:
        db.close()


# ============================================================
# SUBSCRIPTIONS & RECURRING
# ============================================================

@app.get("/api/recurring")
def get_recurring():
    db = SessionLocal()
    try:
        from sqlalchemy import text
        rows = db.execute(text("""
            SELECT
              COALESCE(merchant_clean, counterparty_name, LEFT(description, 50)) as merchant,
              recurring_type,
              COUNT(*) as occurrences,
              AVG(ABS(amount))::numeric(10,2) as avg_amount,
              MIN(ABS(amount))::numeric(10,2) as min_amount,
              MAX(ABS(amount))::numeric(10,2) as max_amount,
              MIN(date) as first_seen,
              MAX(date) as last_seen,
              category_id
            FROM transactions
            WHERE (is_recurring = true OR recurring_type IS NOT NULL)
              AND txn_class = 'expense'
              AND status = 'posted'
            GROUP BY
              COALESCE(merchant_clean, counterparty_name, LEFT(description, 50)),
              recurring_type,
              category_id
            ORDER BY avg_amount DESC
        """)).fetchall()

        return [
            {
                "merchant": r[0],
                "recurring_type": r[1],
                "occurrences": r[2],
                "avg_amount": float(r[3]),
                "min_amount": float(r[4]),
                "max_amount": float(r[5]),
                "first_seen": r[6].isoformat() if r[6] else None,
                "last_seen": r[7].isoformat() if r[7] else None,
                "category_id": r[8],
            }
            for r in rows
        ]
    finally:
        db.close()


# ============================================================
# BALANCES
# ============================================================

@app.post("/api/balances/refresh")
def refresh_balances():
    """Fetch fresh balances from Teller for all accounts."""
    db = SessionLocal()
    try:
        from app.services.teller_client import TellerClient
        from app.db.models import Enrollment
        from decimal import Decimal
        from sqlalchemy.sql import func

        accounts = db.query(Account).filter(Account.status == "open").all()
        results = {}

        for account in accounts:
            enrollment = db.get(Enrollment, account.enrollment_id)
            if not enrollment or enrollment.status != "active":
                continue
            try:
                client = TellerClient(enrollment.access_token)
                bal = client.get_balance(account.id)
                existing = db.get(AccountBalance, account.id)
                if existing:
                    existing.ledger = Decimal(str(bal.get("ledger", 0) or 0))
                    existing.available = Decimal(str(bal.get("available", 0) or 0))
                    existing.fetched_at = func.now()
                else:
                    db.add(AccountBalance(
                        account_id=account.id,
                        ledger=Decimal(str(bal.get("ledger", 0) or 0)),
                        available=Decimal(str(bal.get("available", 0) or 0)),
                    ))
                results[account.name] = {"ok": True, "ledger": bal.get("ledger")}
            except Exception as e:
                results[account.name] = {"error": str(e)}

        db.commit()
        return {"ok": True, "results": results}
    finally:
        db.close()


# ============================================================
# SYNC
# ============================================================

@app.post("/api/sync")
def manual_sync():
    db = SessionLocal()
    try:
        results = sync_all(db)
        return {"ok": True, "results": results}
    finally:
        db.close()


@app.post("/api/classify")
def run_classifier():
    """Re-run classifier on all non-verified transactions."""
    db = SessionLocal()
    try:
        result = classify_all(db, only_unclassified=False)
        return {"ok": True, **result}
    finally:
        db.close()


@app.post("/api/backfill-income")
def backfill_income_endpoint():
    db = SessionLocal()
    try:
        fixed = backfill_income(db)
        return {"ok": True, "fixed": fixed}
    finally:
        db.close()


# ============================================================
# SCHEDULER
# ============================================================

scheduler = BackgroundScheduler()


def scheduled_sync():
    db = SessionLocal()
    try:
        sync_all(db)
    finally:
        db.close()


scheduler.add_job(scheduled_sync, "cron", hour=6, minute=0)
scheduler.start()


# ============================================================
# FRONTEND — serve index.html for all non-API routes
# Must be LAST so it doesn't shadow API routes
# ============================================================

@app.get("/")
@app.get("/ui")
def serve_frontend():
    if os.path.exists(FRONTEND_PATH):
        return FileResponse(FRONTEND_PATH)
    return {"error": "Frontend not found at /app/static/index.html"}
