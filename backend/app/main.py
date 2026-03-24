"""
Budget Duo API — V3

V3 Changes:
- Fixed: Uses FastAPI Depends(get_db) instead of manual session management
- Fixed: Pydantic models for request validation
- Fixed: /api/summary/by-category now includes budget data
- Fixed: Legacy custom_category dual-write removed from main PATCH
- Added: /api/summary supports owner filtering
- Added: /api/rules/from-transaction/{txn_id} for quick rule creation
- Added: /api/recurring returns last_amount for subscriptions, mtd_total for recurring
- Added: Duplicate rule detection on create
"""
from fastapi import FastAPI, HTTPException, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from apscheduler.schedulers.background import BackgroundScheduler
from pydantic import BaseModel, Field
from typing import Optional, Literal
from decimal import Decimal
from enum import Enum
import os
import logging
import re

from app.db.models import Base, Account, Transaction, Category, MerchantRule, AccountBalance
from app.db.session import engine, SessionLocal, get_db
from app.services.sync_service import sync_all, backfill_income
from app.services.classifier import classify_all, classify_transaction

from sqlalchemy.orm import Session

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Budget Duo API v3")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_PATH = "/app/static/index.html"


# ============================================================
# PYDANTIC MODELS
# ============================================================

VALID_TXN_CLASSES = {
    "expense", "income", "internal_transfer", "cc_payment",
    "investment_in", "investment_out", "debt_payment",
    "savings_move", "ignore",
}

VALID_RECURRING_TYPES = {
    "subscription", "utility", "recurring_expense", "one_time", "cancelled",
}

VALID_MATCH_TYPES = {
    "description_contains", "description_starts_with",
    "counterparty_exact", "description_regex",
}


class TransactionUpdate(BaseModel):
    txn_class: Optional[str] = None
    category_id: Optional[str] = None
    subcategory_id: Optional[str] = None
    recurring_type: Optional[str] = None
    merchant_clean: Optional[str] = None


class CategoryCreate(BaseModel):
    name: str
    parent_id: Optional[str] = None
    color: str = "#58a6ff"
    icon: Optional[str] = None
    budget_amount: Optional[float] = None
    sort_order: int = 50
    exclude_from_spending: bool = False


class CategoryUpdate(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None
    icon: Optional[str] = None
    budget_amount: Optional[float] = None
    parent_id: Optional[str] = None
    exclude_from_spending: Optional[bool] = None


class RuleCreate(BaseModel):
    match_type: str = "description_contains"
    match_value: str
    txn_class: Optional[str] = None
    category_id: Optional[str] = None
    subcategory_id: Optional[str] = None
    recurring_type: Optional[str] = None
    merchant_clean: Optional[str] = None
    priority: int = 100
    apply_to_existing: bool = False


class RuleUpdate(BaseModel):
    match_type: Optional[str] = None
    match_value: Optional[str] = None
    txn_class: Optional[str] = None
    category_id: Optional[str] = None
    subcategory_id: Optional[str] = None
    recurring_type: Optional[str] = None
    merchant_clean: Optional[str] = None
    priority: Optional[int] = None
    is_active: Optional[bool] = None
    apply_to_existing: bool = False


class RuleFromTransaction(BaseModel):
    txn_class: Optional[str] = None
    category_id: Optional[str] = None
    subcategory_id: Optional[str] = None
    recurring_type: Optional[str] = None
    merchant_clean: Optional[str] = None
    match_type: str = "description_contains"
    apply_to_existing: bool = True


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
def health():
    return {"status": "ok", "version": "3.0"}


# ============================================================
# ACCOUNTS
# ============================================================

@app.get("/api/accounts")
def get_accounts(db: Session = Depends(get_db)):
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
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    uncategorized: Optional[bool] = None,
    limit: int = Query(default=500, le=2000),
    offset: int = 0,
    db: Session = Depends(get_db),
):
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
    if uncategorized:
        q = q.filter(
            Transaction.category_id == None,
            Transaction.txn_class == "expense",
            Transaction.status == "posted",
        )
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
    if date_from:
        q = q.filter(Transaction.date >= date_from)
    if date_to:
        q = q.filter(Transaction.date <= date_to)

    q = q.order_by(Transaction.date.desc()).offset(offset).limit(limit)
    txns = q.all()

    return [_serialize_txn(t) for t in txns]


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
        "recurring_type": t.recurring_type,
        "status": t.status,
        "is_income": t.is_income,
        "is_recurring": t.is_recurring,
        "user_verified": t.user_verified,
        "rule_id": t.rule_id,
        "type": t.txn_type,
    }


@app.patch("/api/transactions/{txn_id}")
def update_transaction(txn_id: str, body: TransactionUpdate, db: Session = Depends(get_db)):
    """
    Update classification, category, or recurring type on a transaction.
    Validates txn_class and recurring_type values.
    Always marks as user_verified=True so auto-classifier won't overwrite.
    """
    txn = db.get(Transaction, txn_id)
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")

    if body.txn_class is not None:
        if body.txn_class and body.txn_class not in VALID_TXN_CLASSES:
            raise HTTPException(status_code=400, detail=f"Invalid txn_class: {body.txn_class}")
        txn.txn_class = body.txn_class or None
        # V3: Sync is_income flag
        from app.services.classifier import sync_flags
        sync_flags(txn)

    if body.category_id is not None:
        txn.category_id = body.category_id or None

    if body.subcategory_id is not None:
        txn.subcategory_id = body.subcategory_id or None

    if body.recurring_type is not None:
        if body.recurring_type and body.recurring_type not in VALID_RECURRING_TYPES:
            raise HTTPException(status_code=400, detail=f"Invalid recurring_type: {body.recurring_type}")
        txn.recurring_type = body.recurring_type or None
        txn.is_recurring = bool(body.recurring_type)

    if body.merchant_clean is not None:
        txn.merchant_clean = body.merchant_clean or None

    txn.user_verified = True
    db.commit()
    return _serialize_txn(txn)


# ============================================================
# CATEGORIES
# ============================================================

@app.get("/api/categories")
def get_categories(db: Session = Depends(get_db)):
    cats = db.query(Category).order_by(
        Category.sort_order.asc(), Category.name.asc()
    ).all()
    return [_serialize_category(c) for c in cats]


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
def create_category(body: CategoryCreate, db: Session = Depends(get_db)):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name required")

    slug = re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')
    base_slug = slug
    i = 2
    while db.get(Category, slug):
        slug = f"{base_slug}_{i}"
        i += 1

    if body.parent_id and not db.get(Category, body.parent_id):
        raise HTTPException(status_code=400, detail="Parent category not found")

    cat = Category(
        id=slug,
        name=name,
        parent_id=body.parent_id,
        color=body.color,
        icon=body.icon,
        is_system=False,
        budget_amount=body.budget_amount,
        sort_order=body.sort_order,
        exclude_from_spending=body.exclude_from_spending,
    )
    db.add(cat)
    db.commit()
    return _serialize_category(cat)


@app.patch("/api/categories/{cat_id}")
def update_category(cat_id: str, body: CategoryUpdate, db: Session = Depends(get_db)):
    cat = db.get(Category, cat_id)
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")

    if body.name is not None:
        cat.name = body.name
    if body.color is not None:
        cat.color = body.color
    if body.icon is not None:
        cat.icon = body.icon
    if body.budget_amount is not None:
        cat.budget_amount = body.budget_amount
    if body.parent_id is not None:
        cat.parent_id = body.parent_id or None
    if body.exclude_from_spending is not None:
        cat.exclude_from_spending = body.exclude_from_spending

    db.commit()
    return _serialize_category(cat)


@app.delete("/api/categories/{cat_id}")
def delete_category(cat_id: str, db: Session = Depends(get_db)):
    cat = db.get(Category, cat_id)
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")
    if cat.is_system:
        raise HTTPException(status_code=400, detail="Cannot delete system categories")

    children = db.query(Category).filter(Category.parent_id == cat_id).count()
    if children > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Category has {children} subcategories. Delete or reassign them first."
        )

    # Count affected transactions for the response
    affected = db.query(Transaction).filter(
        (Transaction.category_id == cat_id) | (Transaction.subcategory_id == cat_id)
    ).count()

    db.query(Transaction).filter(Transaction.category_id == cat_id).update({
        "category_id": None, "user_verified": False
    })
    db.query(Transaction).filter(Transaction.subcategory_id == cat_id).update({
        "subcategory_id": None
    })

    db.delete(cat)
    db.commit()
    return {"ok": True, "transactions_unassigned": affected}


# ============================================================
# MERCHANT RULES
# ============================================================

@app.get("/api/rules")
def get_rules(db: Session = Depends(get_db)):
    rules = db.query(MerchantRule).order_by(
        MerchantRule.priority.asc(), MerchantRule.match_count.desc()
    ).all()
    return [_serialize_rule(r) for r in rules]


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


def _find_existing_rule(db: Session, match_type: str, match_value: str) -> Optional[MerchantRule]:
    """Check for duplicate/overlapping rules."""
    return db.query(MerchantRule).filter(
        MerchantRule.match_type == match_type,
        MerchantRule.match_value.ilike(match_value),
        MerchantRule.is_active == True,
    ).first()


@app.post("/api/rules")
def create_rule(body: RuleCreate, db: Session = Depends(get_db)):
    """Create a user merchant rule. Checks for duplicates first."""
    match_value = body.match_value.strip()
    if not match_value:
        raise HTTPException(status_code=400, detail="match_value required")

    if body.match_type not in VALID_MATCH_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid match_type: {body.match_type}")
    if body.txn_class and body.txn_class not in VALID_TXN_CLASSES:
        raise HTTPException(status_code=400, detail=f"Invalid txn_class: {body.txn_class}")
    if body.recurring_type and body.recurring_type not in VALID_RECURRING_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid recurring_type: {body.recurring_type}")

    # V3: Check for existing rule with same match
    existing = _find_existing_rule(db, body.match_type, match_value)
    if existing:
        # Update the existing rule instead of creating a duplicate
        if body.txn_class:
            existing.txn_class = body.txn_class
        if body.category_id:
            existing.category_id = body.category_id
        if body.subcategory_id:
            existing.subcategory_id = body.subcategory_id
        if body.recurring_type is not None:
            existing.recurring_type = body.recurring_type
        if body.merchant_clean:
            existing.merchant_clean = body.merchant_clean
        existing.is_active = True
        db.commit()

        if body.apply_to_existing:
            classify_all(db, only_unclassified=False)

        return {**_serialize_rule(existing), "was_duplicate": True}

    slug = "rule_user_" + re.sub(r'[^a-z0-9]+', '_', match_value.lower())[:40]
    base = slug
    i = 2
    while db.get(MerchantRule, slug):
        slug = f"{base}_{i}"
        i += 1

    rule = MerchantRule(
        id=slug,
        match_type=body.match_type,
        match_value=match_value,
        txn_class=body.txn_class,
        category_id=body.category_id,
        subcategory_id=body.subcategory_id,
        recurring_type=body.recurring_type,
        merchant_clean=body.merchant_clean,
        priority=body.priority,
        is_system=False,
        is_active=True,
    )
    db.add(rule)
    db.commit()

    if body.apply_to_existing:
        classify_all(db, only_unclassified=False)

    return _serialize_rule(rule)


@app.post("/api/rules/from-transaction/{txn_id}")
def create_rule_from_transaction(
    txn_id: str,
    body: RuleFromTransaction,
    db: Session = Depends(get_db),
):
    """
    Create a rule pre-populated from an existing transaction.
    Uses the transaction's description/counterparty as the match value.
    """
    txn = db.get(Transaction, txn_id)
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")

    # Determine the best match value
    if body.match_type == "counterparty_exact" and txn.counterparty_name:
        match_value = txn.counterparty_name
    else:
        # Use a cleaned version of the description
        # Strip common noise prefixes
        desc = txn.description.strip()
        match_value = desc

    if not match_value:
        raise HTTPException(status_code=400, detail="Could not determine match value from transaction")

    # Build the rule
    rule_body = RuleCreate(
        match_type=body.match_type,
        match_value=match_value,
        txn_class=body.txn_class or txn.txn_class,
        category_id=body.category_id or txn.category_id,
        subcategory_id=body.subcategory_id or txn.subcategory_id,
        recurring_type=body.recurring_type or txn.recurring_type,
        merchant_clean=body.merchant_clean or txn.merchant_clean or txn.counterparty_name,
        apply_to_existing=body.apply_to_existing,
    )

    return create_rule(rule_body, db)


@app.patch("/api/rules/{rule_id}")
def update_rule(rule_id: str, body: RuleUpdate, db: Session = Depends(get_db)):
    rule = db.get(MerchantRule, rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")

    if body.match_type is not None:
        rule.match_type = body.match_type
    if body.match_value is not None:
        rule.match_value = body.match_value
    if body.txn_class is not None:
        rule.txn_class = body.txn_class
    if body.category_id is not None:
        rule.category_id = body.category_id
    if body.subcategory_id is not None:
        rule.subcategory_id = body.subcategory_id
    if body.recurring_type is not None:
        rule.recurring_type = body.recurring_type
    if body.merchant_clean is not None:
        rule.merchant_clean = body.merchant_clean
    if body.priority is not None:
        rule.priority = body.priority
    if body.is_active is not None:
        rule.is_active = body.is_active

    db.commit()

    if body.apply_to_existing:
        classify_all(db, only_unclassified=False)

    return _serialize_rule(rule)


@app.delete("/api/rules/{rule_id}")
def delete_rule(rule_id: str, db: Session = Depends(get_db)):
    rule = db.get(MerchantRule, rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    if rule.is_system:
        raise HTTPException(status_code=400, detail="Cannot delete system rules. Disable them instead.")
    db.delete(rule)
    db.commit()
    return {"ok": True}


# ============================================================
# SUMMARY / DASHBOARD
# ============================================================

@app.get("/api/summary")
def get_summary(owner: Optional[str] = None, db: Session = Depends(get_db)):
    """
    Dashboard summary. Optionally filter by owner (sam/jess/joint).
    V3: Added owner filtering support.
    """
    from sqlalchemy import text
    from datetime import date

    today = date.today()
    month_start = today.replace(day=1).isoformat()

    owner_join = ""
    owner_filter = ""
    params = {"month_start": month_start}

    if owner:
        owner_join = "JOIN accounts a ON a.id = t.account_id"
        owner_filter = "AND a.owner = :owner"
        params["owner"] = owner

    # Real spending MTD
    spending_row = db.execute(text(f"""
        SELECT COALESCE(SUM(ABS(t.amount)), 0)
        FROM transactions t
        {owner_join}
        WHERE t.txn_class = 'expense'
          AND t.amount < 0
          AND t.date >= :month_start
          AND t.status != 'pending'
          {owner_filter}
    """), params).fetchone()

    # Real income MTD
    income_row = db.execute(text(f"""
        SELECT COALESCE(SUM(t.amount), 0)
        FROM transactions t
        {owner_join}
        WHERE t.txn_class = 'income'
          AND t.amount > 0
          AND t.date >= :month_start
          AND t.status != 'pending'
          {owner_filter}
    """), params).fetchone()

    # Subscriptions monthly total
    sub_row = db.execute(text(f"""
        SELECT COALESCE(SUM(ABS(t.amount)), 0), COUNT(DISTINCT t.merchant_clean)
        FROM transactions t
        {owner_join}
        WHERE t.recurring_type = 'subscription'
          AND t.txn_class = 'expense'
          AND t.date >= :month_start
          {owner_filter}
    """), params).fetchone()

    # CC payments MTD
    cc_row = db.execute(text(f"""
        SELECT COALESCE(SUM(ABS(t.amount)), 0)
        FROM transactions t
        {owner_join}
        WHERE t.txn_class = 'cc_payment'
          AND t.date >= :month_start
          {owner_filter}
    """), params).fetchone()

    # Account balances (these aren't filtered by date, but can be by owner)
    bal_filter = f"AND a.owner = '{owner}'" if owner else ""
    balances = db.execute(text(f"""
        SELECT a.type, a.is_savings, COALESCE(ab.ledger, 0) as ledger
        FROM accounts a
        LEFT JOIN account_balances ab ON ab.account_id = a.id
        WHERE a.status = 'open'
        {bal_filter}
    """)).fetchall()

    cash_on_hand = sum(float(r[2]) for r in balances if r[0] == 'depository' and not r[1])
    savings_total = sum(float(r[2]) for r in balances if r[1])
    credit_debt = sum(abs(float(r[2])) for r in balances if r[0] == 'credit')

    # Uncategorized count
    uncat_row = db.execute(text(f"""
        SELECT COUNT(*)
        FROM transactions t
        {owner_join}
        WHERE t.txn_class = 'expense'
          AND t.category_id IS NULL
          AND t.user_verified = FALSE
          AND t.status = 'posted'
          AND t.date >= :month_start
          {owner_filter}
    """), params).fetchone()

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
        "owner_filter": owner,
    }


@app.get("/api/summary/by-category")
def get_spending_by_category(
    owner: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    Spending breakdown by category for current month.
    V3: Now includes budget_amount and budget progress.
    """
    from sqlalchemy import text
    from datetime import date

    month_start = date.today().replace(day=1).isoformat()

    owner_join = ""
    owner_filter = ""
    params = {"month_start": month_start}

    if owner:
        owner_join = "JOIN accounts a ON a.id = t.account_id"
        owner_filter = "AND a.owner = :owner"
        params["owner"] = owner

    rows = db.execute(text(f"""
        SELECT
          COALESCE(t.category_id, 'uncategorized') as cat_id,
          COALESCE(c.name, 'Uncategorized') as cat_name,
          COALESCE(c.color, '#484f58') as color,
          COALESCE(cp.name, c.name, 'Uncategorized') as parent_name,
          COALESCE(cp.id, c.id, 'uncategorized') as parent_id,
          COUNT(*) as txn_count,
          SUM(ABS(t.amount))::numeric(12,2) as total,
          COALESCE(c.budget_amount, cp.budget_amount) as budget_amount
        FROM transactions t
        LEFT JOIN categories c ON c.id = t.category_id
        LEFT JOIN categories cp ON cp.id = c.parent_id
        {owner_join}
        WHERE t.txn_class = 'expense'
          AND t.amount < 0
          AND t.date >= :month_start
          AND t.status != 'pending'
          {owner_filter}
        GROUP BY
          COALESCE(t.category_id, 'uncategorized'),
          c.name, c.color, cp.name, cp.id, c.budget_amount, cp.budget_amount
        ORDER BY total DESC
    """), params).fetchall()

    return [
        {
            "category_id": r[0],
            "category_name": r[1],
            "color": r[2],
            "parent_name": r[3],
            "parent_id": r[4],
            "txn_count": r[5],
            "total": float(r[6]),
            "budget_amount": float(r[7]) if r[7] else None,
            "budget_pct": round(float(r[6]) / float(r[7]) * 100, 1) if r[7] and float(r[7]) > 0 else None,
        }
        for r in rows
    ]


@app.get("/api/summary/monthly")
def get_monthly_summary(months: int = 6, db: Session = Depends(get_db)):
    """Month-over-month spending vs income."""
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


@app.get("/api/summary/by-owner")
def get_spending_by_owner(db: Session = Depends(get_db)):
    """Sam vs Jess vs Joint spending breakdown for current month."""
    from sqlalchemy import text
    from datetime import date

    month_start = date.today().replace(day=1).isoformat()

    rows = db.execute(text("""
        SELECT
          a.owner,
          SUM(CASE WHEN t.txn_class = 'expense' AND t.amount < 0
              THEN ABS(t.amount) ELSE 0 END)::numeric(12,2) as spending,
          SUM(CASE WHEN t.txn_class = 'income' AND t.amount > 0
              THEN t.amount ELSE 0 END)::numeric(12,2) as income,
          COUNT(CASE WHEN t.txn_class = 'expense' THEN 1 END) as txn_count
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        WHERE t.date >= :month_start
          AND t.status != 'pending'
        GROUP BY a.owner
        ORDER BY spending DESC
    """), {"month_start": month_start}).fetchall()

    return [
        {
            "owner": r[0],
            "spending": float(r[1]),
            "income": float(r[2]),
            "txn_count": r[3],
        }
        for r in rows
    ]


# ============================================================
# SUBSCRIPTIONS & RECURRING
# ============================================================

@app.get("/api/recurring")
def get_recurring(db: Session = Depends(get_db)):
    """
    V3: Returns split data for subscriptions vs recurring merchants.
    - Subscriptions: last charge amount + date (not avg)
    - Recurring merchants: MTD total + count this month
    """
    from sqlalchemy import text
    from datetime import date

    month_start = date.today().replace(day=1).isoformat()

    rows = db.execute(text("""
        WITH merchant_stats AS (
          SELECT
            COALESCE(merchant_clean, counterparty_name, LEFT(description, 50)) as merchant,
            recurring_type,
            category_id,
            COUNT(*) as total_occurrences,
            -- Last charge info (for subscriptions)
            (ARRAY_AGG(ABS(amount) ORDER BY date DESC))[1] as last_amount,
            MAX(date) as last_seen,
            MIN(date) as first_seen,
            -- This month stats (for recurring merchants)
            COUNT(CASE WHEN date >= :month_start THEN 1 END) as mtd_count,
            COALESCE(SUM(CASE WHEN date >= :month_start THEN ABS(amount) END), 0)::numeric(10,2) as mtd_total,
            -- Averages
            AVG(ABS(amount))::numeric(10,2) as avg_amount,
            MIN(ABS(amount))::numeric(10,2) as min_amount,
            MAX(ABS(amount))::numeric(10,2) as max_amount
          FROM transactions
          WHERE (is_recurring = true OR recurring_type IS NOT NULL)
            AND txn_class = 'expense'
            AND status = 'posted'
          GROUP BY
            COALESCE(merchant_clean, counterparty_name, LEFT(description, 50)),
            recurring_type,
            category_id
        )
        SELECT * FROM merchant_stats
        ORDER BY
          CASE WHEN recurring_type = 'subscription' THEN 0
               WHEN recurring_type = 'utility' THEN 1
               ELSE 2 END,
          last_amount DESC NULLS LAST
    """), {"month_start": month_start}).fetchall()

    return [
        {
            "merchant": r[0],
            "recurring_type": r[1],
            "category_id": r[2],
            "total_occurrences": r[3],
            "last_amount": float(r[4]) if r[4] else None,
            "last_seen": r[5].isoformat() if r[5] else None,
            "first_seen": r[6].isoformat() if r[6] else None,
            "mtd_count": r[7],
            "mtd_total": float(r[8]),
            "avg_amount": float(r[9]) if r[9] else None,
            "min_amount": float(r[10]) if r[10] else None,
            "max_amount": float(r[11]) if r[11] else None,
        }
        for r in rows
    ]


# ============================================================
# BALANCES
# ============================================================

@app.post("/api/balances/refresh")
def refresh_balances(db: Session = Depends(get_db)):
    """Fetch fresh balances from Teller for all accounts."""
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


# ============================================================
# SYNC
# ============================================================

@app.post("/api/sync")
def manual_sync(db: Session = Depends(get_db)):
    results = sync_all(db)
    return {"ok": True, "results": results}


@app.post("/api/classify")
def run_classifier(db: Session = Depends(get_db)):
    """Re-run classifier on all non-verified transactions."""
    result = classify_all(db, only_unclassified=False)
    return {"ok": True, **result}


@app.post("/api/backfill-income")
def backfill_income_endpoint(db: Session = Depends(get_db)):
    fixed = backfill_income(db)
    return {"ok": True, "fixed": fixed}


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