"""
Budget Duo API — V4.1

V4.1 Changes:
- cat_l1 is now auto-derived from txn_class on every PATCH — never manually set by user
- cat_l5 added for dynamic entry (trip/car/project) at 5th category level
- All other V4 logic unchanged

V4.1.1:
- Sync scheduler changed from daily 6am to every hour
"""
from fastapi import FastAPI, HTTPException, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from apscheduler.schedulers.background import BackgroundScheduler
from pydantic import BaseModel
from typing import Optional
from decimal import Decimal
from datetime import date, timedelta
import calendar
import os
import re
import logging
import uuid

from app.db.models import (
    Base, Account, Transaction, Category, DynamicEntry,
    MerchantRule, AccountBalance, Enrollment, SyncLog
)
from app.db.session import engine, SessionLocal, get_db
from app.services.sync_service import sync_all
from sqlalchemy.orm import Session
from sqlalchemy import text, or_, func

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Budget Duo API v4.1.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_PATH = "/app/static/index.html"

SAVINGS_ACCOUNT_LAST_FOUR = {"9992", "0224", "5994"}
BILLS_ONLY_LAST_FOUR = {"0687"}

VALID_TXN_CLASSES = {
    "income", "expense", "savings_in", "savings_out",
    "investment_in", "investment_out", "subscription",
    "internal_transfer", "cc_payment", "ignore",
}

VALID_RECURRING_TYPES = {
    "subscription", "utility", "recurring_expense", "one_time", "cancelled",
}

VALID_MATCH_TYPES = {
    "description_contains", "description_starts_with",
    "counterparty_exact", "description_regex",
}

VALID_ENTRY_TYPES = {"car", "project", "trip"}

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




# ============================================================
# PYDANTIC MODELS
# ============================================================

class TransactionUpdate(BaseModel):
    txn_class:      Optional[str] = None
    cat_l2:         Optional[str] = None
    cat_l3:         Optional[str] = None
    cat_l4:         Optional[str] = None
    cat_l5:         Optional[str] = None
    recurring_type: Optional[str] = None
    merchant_clean: Optional[str] = None


class CategoryCreate(BaseModel):
    name:         str
    parent_id:    Optional[str] = None
    color:        str = "#3d9eff"
    budget_amount: Optional[float] = None
    sort_order:   int = 50


class CategoryUpdate(BaseModel):
    name:          Optional[str]   = None
    color:         Optional[str]   = None
    budget_amount: Optional[float] = None
    sort_order:    Optional[int]   = None


class DynamicEntryCreate(BaseModel):
    entry_type:  str
    name:        str
    description: Optional[str] = None
    sort_order:  int = 50


class DynamicEntryUpdate(BaseModel):
    name:        Optional[str]  = None
    description: Optional[str]  = None
    is_active:   Optional[bool] = None
    sort_order:  Optional[int]  = None


class RuleCreate(BaseModel):
    match_type:     str = "description_contains"
    match_value:    str
    txn_class:      Optional[str] = None
    cat_l1:         Optional[str] = None
    cat_l2:         Optional[str] = None
    cat_l3:         Optional[str] = None
    cat_l4:         Optional[str] = None
    cat_l5:         Optional[str] = None
    recurring_type: Optional[str] = None
    merchant_clean: Optional[str] = None
    priority:       int = 100
    apply_to_existing: bool = False


class RuleUpdate(BaseModel):
    match_type:     Optional[str]  = None
    match_value:    Optional[str]  = None
    txn_class:      Optional[str]  = None
    cat_l1:         Optional[str]  = None
    cat_l2:         Optional[str]  = None
    cat_l3:         Optional[str]  = None
    cat_l4:         Optional[str]  = None
    cat_l5:         Optional[str]  = None
    recurring_type: Optional[str]  = None
    merchant_clean: Optional[str]  = None
    priority:       Optional[int]  = None
    is_active:      Optional[bool] = None
    apply_to_existing: bool = False


# ============================================================
# HELPERS
# ============================================================

def _period_bounds(period: str, year: int, month: Optional[int]) -> tuple[str, str]:
    if period == "year":
        return f"{year}-01-01", f"{year}-12-31"
    else:
        m = month or date.today().month
        last_day = calendar.monthrange(year, m)[1]
        return f"{year}-{m:02d}-01", f"{year}-{m:02d}-{last_day:02d}"


def _serialize_txn(t: Transaction) -> dict:
    acct = t.account
    return {
        "id":             t.id,
        "account_id":     t.account_id,
        "account_name":   acct.name if acct else None,
        "account_last4":  acct.last_four if acct else None,
        "institution":    acct.institution_name if acct else None,
        "owner":          acct.owner if acct else None,
        "amount":         float(t.amount),
        "date":           t.date.isoformat(),
        "description":    t.description,
        "counterparty":   t.counterparty_name,
        "merchant_clean": t.merchant_clean,
        "txn_class":      t.txn_class,
        "cat_l1":         t.cat_l1,
        "cat_l2":         t.cat_l2,
        "cat_l3":         t.cat_l3,
        "cat_l4":         t.cat_l4,
        "cat_l5":         t.cat_l5,
        "recurring_type": t.recurring_type,
        "status":         t.status,
        "is_income":      t.is_income,
        "is_recurring":   t.is_recurring,
        "user_verified":  t.user_verified,
        "rule_id":        t.rule_id,
    }


def _serialize_category(c: Category) -> dict:
    return {
        "id":               c.id,
        "name":             c.name,
        "parent_id":        c.parent_id,
        "level":            c.level,
        "color":            c.color,
        "is_system":        c.is_system,
        "is_dynamic_parent": c.is_dynamic_parent,
        "entry_type":       c.entry_type,
        "budget_amount":    float(c.budget_amount) if c.budget_amount else None,
        "sort_order":       c.sort_order,
    }


def _serialize_rule(r: MerchantRule) -> dict:
    return {
        "id":             r.id,
        "match_type":     r.match_type,
        "match_value":    r.match_value,
        "txn_class":      r.txn_class,
        "cat_l1":         r.cat_l1,
        "cat_l2":         r.cat_l2,
        "cat_l3":         r.cat_l3,
        "cat_l4":         r.cat_l4,
        "cat_l5":         getattr(r, 'cat_l5', None),
        "recurring_type": r.recurring_type,
        "merchant_clean": r.merchant_clean,
        "priority":       r.priority,
        "is_system":      r.is_system,
        "is_active":      r.is_active,
        "match_count":    r.match_count,
        "created_at":     r.created_at.isoformat() if r.created_at else None,
    }


def _apply_rule_to_existing(db: Session, rule: MerchantRule):
    txns = db.query(Transaction).filter(Transaction.user_verified == False).all()
    updated = 0
    for t in txns:
        if _rule_matches(rule, t):
            if rule.txn_class:
                t.txn_class = rule.txn_class
                t.cat_l1 = TXN_CLASS_TO_CAT_L1.get(rule.txn_class, t.cat_l1)
            if rule.cat_l2:        t.cat_l2 = rule.cat_l2
            if rule.cat_l3:        t.cat_l3 = rule.cat_l3
            if rule.cat_l4:        t.cat_l4 = rule.cat_l4
            if rule.recurring_type:
                t.recurring_type = rule.recurring_type
                t.is_recurring = True
            if rule.merchant_clean: t.merchant_clean = rule.merchant_clean
            updated += 1
    db.commit()
    return updated


def _rule_matches(rule: MerchantRule, t: Transaction) -> bool:
    desc = (t.description or "").lower()
    val  = rule.match_value.lower()
    if rule.match_type == "description_contains":
        return val in desc
    if rule.match_type == "description_starts_with":
        return desc.startswith(val)
    if rule.match_type == "counterparty_exact":
        return (t.counterparty_name or "").lower() == val
    return False


def _auto_classify(db: Session, t: Transaction):
    if t.user_verified:
        return

    rules = db.query(MerchantRule).filter(
        MerchantRule.is_active == True
    ).order_by(MerchantRule.priority.asc()).all()

    for rule in rules:
        if _rule_matches(rule, t):
            if rule.txn_class:
                t.txn_class = rule.txn_class
                t.cat_l1 = TXN_CLASS_TO_CAT_L1.get(rule.txn_class, t.cat_l1)
            if rule.cat_l2:        t.cat_l2 = rule.cat_l2
            if rule.cat_l3:        t.cat_l3 = rule.cat_l3
            if rule.cat_l4:        t.cat_l4 = rule.cat_l4
            if rule.recurring_type:
                t.recurring_type = rule.recurring_type
                t.is_recurring = True
            if rule.merchant_clean: t.merchant_clean = rule.merchant_clean
            t.rule_id = rule.id
            rule.match_count = (rule.match_count or 0) + 1
            return

    if t.is_income and t.amount > 0:
        t.txn_class = "income"
        t.cat_l1 = "inc"
        return

    acct = t.account
    if acct and acct.is_savings and not acct.exclude_from_savings:
        # On the savings account itself: positive = money arrived = savings_in
        t.txn_class = "savings_in" if t.amount > 0 else "savings_out"
        t.cat_l1 = TXN_CLASS_TO_CAT_L1[t.txn_class]
        return

    if acct and acct.exclude_from_savings:
        t.txn_class = "expense" if t.amount < 0 else "internal_transfer"
        t.cat_l1 = TXN_CLASS_TO_CAT_L1[t.txn_class]
        return

    # For checking/credit accounts: negative transfers toward savings = savings_in
    # (money leaving checking to go into savings pool)
    if acct and acct.type == "credit":
        t.txn_class = "expense" if t.amount < 0 else "cc_payment"
    elif t.amount < 0:
        t.txn_class = "expense"
    else:
        t.txn_class = "internal_transfer"
    t.cat_l1 = TXN_CLASS_TO_CAT_L1.get(t.txn_class, t.cat_l1)


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
def health():
    return {"status": "ok", "version": "4.1.1"}


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
            "id":                   a.id,
            "name":                 a.name,
            "institution":          a.institution_name,
            "type":                 a.type,
            "subtype":              a.subtype,
            "last_four":            a.last_four,
            "owner":                a.owner,
            "role":                 a.role,
            "is_bills_only":        a.is_bills_only,
            "is_savings":           a.is_savings,
            "exclude_from_savings": a.exclude_from_savings,
            "balance_ledger":       float(bal.ledger)    if bal and bal.ledger    else None,
            "balance_available":    float(bal.available) if bal and bal.available else None,
            "balance_fetched_at":   bal.fetched_at.isoformat() if bal and bal.fetched_at else None,
        })
    return result


# ============================================================
# TRANSACTIONS
# ============================================================

@app.get("/api/transactions")
def get_transactions(
    account_id:     Optional[str]  = None,
    owner:          Optional[str]  = None,
    txn_class:      Optional[str]  = None,
    cat_l1:         Optional[str]  = None,
    cat_l2:         Optional[str]  = None,
    cat_l3:         Optional[str]  = None,
    cat_l4:         Optional[str]  = None,
    cat_l5:         Optional[str]  = None,
    recurring_type: Optional[str]  = None,
    search:         Optional[str]  = None,
    date_from:      Optional[str]  = None,
    date_to:        Optional[str]  = None,
    uncategorized:  Optional[bool] = None,
    verified:       Optional[bool] = None,
    limit:          int = Query(default=500, le=2000),
    offset:         int = 0,
    db: Session = Depends(get_db),
):
    q = db.query(Transaction).join(Account)

    if account_id:     q = q.filter(Transaction.account_id == account_id)
    if owner:          q = q.filter(Account.owner == owner)
    if txn_class:      q = q.filter(Transaction.txn_class == txn_class)
    if cat_l1:         q = q.filter(Transaction.cat_l1 == cat_l1)
    if cat_l2:         q = q.filter(Transaction.cat_l2 == cat_l2)
    if cat_l3:         q = q.filter(Transaction.cat_l3 == cat_l3)
    if cat_l4:         q = q.filter(Transaction.cat_l4 == cat_l4)
    if cat_l5:         q = q.filter(Transaction.cat_l5 == cat_l5)
    if recurring_type: q = q.filter(Transaction.recurring_type == recurring_type)
    if uncategorized:
        q = q.filter(
            Transaction.txn_class.in_(["expense", "subscription"]),
            Transaction.cat_l2 == None,
            Transaction.status == "posted",
        )
    if verified is not None:
        q = q.filter(Transaction.user_verified == verified)
    if search:
        s = f"%{search.lower()}%"
        q = q.filter(or_(
            func.lower(Transaction.description).like(s),
            func.lower(Transaction.counterparty_name).like(s),
            func.lower(Transaction.merchant_clean).like(s),
        ))
    if date_from: q = q.filter(Transaction.date >= date_from)
    if date_to:   q = q.filter(Transaction.date <= date_to)

    txns = q.order_by(Transaction.date.desc()).offset(offset).limit(limit).all()
    return [_serialize_txn(t) for t in txns]


@app.patch("/api/transactions/{txn_id}")
def update_transaction(
    txn_id: str,
    body:   TransactionUpdate,
    db:     Session = Depends(get_db),
):
    txn = db.get(Transaction, txn_id)
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")

    if body.txn_class is not None:
        if body.txn_class and body.txn_class not in VALID_TXN_CLASSES:
            raise HTTPException(status_code=400, detail=f"Invalid txn_class: {body.txn_class}")
        txn.txn_class = body.txn_class or None
        txn.is_income = body.txn_class == "income"
        txn.cat_l1 = TXN_CLASS_TO_CAT_L1.get(body.txn_class) if body.txn_class else None
        # Reset category tree when classification changes
        txn.cat_l2 = None
        txn.cat_l3 = None
        txn.cat_l4 = None
        txn.cat_l5 = None

    if body.cat_l2 is not None:
        txn.cat_l2 = body.cat_l2 or None
    if body.cat_l3 is not None:
        txn.cat_l3 = body.cat_l3 or None
        txn.cat_l4 = None
        txn.cat_l5 = None
    if body.cat_l4 is not None:
        txn.cat_l4 = body.cat_l4 or None
        txn.cat_l5 = None
    if body.cat_l5 is not None:
        txn.cat_l5 = body.cat_l5 or None

    if body.recurring_type is not None:
        if body.recurring_type and body.recurring_type not in VALID_RECURRING_TYPES:
            raise HTTPException(status_code=400, detail=f"Invalid recurring_type: {body.recurring_type}")
        txn.recurring_type = body.recurring_type or None
        txn.is_recurring = bool(body.recurring_type)

    if body.merchant_clean is not None:
        txn.merchant_clean = body.merchant_clean or None

    txn.user_verified = True
    db.commit()
    db.refresh(txn)
    return _serialize_txn(txn)


# ============================================================
# SUMMARY / DASHBOARD
# ============================================================

@app.get("/api/summary")
def get_summary(
    period: str = Query(default="month", regex="^(month|year)$"),
    year:   int = Query(default=None),
    month:  Optional[int] = Query(default=None, ge=1, le=12),
    owner:  Optional[str] = None,
    db: Session = Depends(get_db),
):
    today = date.today()
    if year is None:
        year = today.year
    if period == "month" and month is None:
        month = today.month

    date_from, date_to = _period_bounds(period, year, month)

    owner_filter = "AND a.owner = :owner" if owner else ""
    params: dict = {"date_from": date_from, "date_to": date_to}
    if owner:
        params["owner"] = owner

    income = db.execute(text(f"""
        SELECT COALESCE(SUM(t.amount), 0)
        FROM transactions t JOIN accounts a ON a.id = t.account_id
        WHERE t.txn_class = 'income' AND t.amount > 0
          AND t.date BETWEEN :date_from AND :date_to AND t.status != 'pending'
          {owner_filter}
    """), params).scalar()

    spending = db.execute(text(f"""
        SELECT COALESCE(SUM(ABS(t.amount)), 0)
        FROM transactions t JOIN accounts a ON a.id = t.account_id
        WHERE t.txn_class IN ('expense', 'subscription') AND t.amount < 0
          AND t.date BETWEEN :date_from AND :date_to AND t.status != 'pending'
          {owner_filter}
    """), params).scalar()

    savings_in = db.execute(text(f"""
        SELECT COALESCE(SUM(t.amount), 0)
        FROM transactions t JOIN accounts a ON a.id = t.account_id
        WHERE t.txn_class = 'savings_in'
          AND t.date BETWEEN :date_from AND :date_to AND t.status != 'pending'
          AND a.exclude_from_savings = FALSE {owner_filter}
    """), params).scalar()

    savings_out = db.execute(text(f"""
        SELECT COALESCE(SUM(ABS(t.amount)), 0)
        FROM transactions t JOIN accounts a ON a.id = t.account_id
        WHERE t.txn_class = 'savings_out'
          AND t.date BETWEEN :date_from AND :date_to AND t.status != 'pending'
          AND a.exclude_from_savings = FALSE {owner_filter}
    """), params).scalar()

    inv_in = db.execute(text(f"""
        SELECT COALESCE(SUM(t.amount), 0)
        FROM transactions t JOIN accounts a ON a.id = t.account_id
        WHERE t.txn_class = 'investment_in'
          AND t.date BETWEEN :date_from AND :date_to AND t.status != 'pending'
          {owner_filter}
    """), params).scalar()

    inv_out = db.execute(text(f"""
        SELECT COALESCE(SUM(ABS(t.amount)), 0)
        FROM transactions t JOIN accounts a ON a.id = t.account_id
        WHERE t.txn_class = 'investment_out'
          AND t.date BETWEEN :date_from AND :date_to AND t.status != 'pending'
          {owner_filter}
    """), params).scalar()

    sub_row = db.execute(text(f"""
        SELECT COALESCE(SUM(ABS(t.amount)), 0), COUNT(*),
               COALESCE(MAX(ABS(t.amount)), 0),
               (ARRAY_AGG(COALESCE(t.merchant_clean, t.counterparty_name, LEFT(t.description,40))
                ORDER BY ABS(t.amount) DESC))[1]
        FROM transactions t JOIN accounts a ON a.id = t.account_id
        WHERE t.recurring_type = 'subscription'
          AND t.txn_class IN ('expense', 'subscription')
          AND t.date BETWEEN :date_from AND :date_to AND t.status != 'pending'
          {owner_filter}
    """), params).fetchone()

    bal_filter = "AND a.owner = :owner" if owner else ""
    bp = {"owner": owner} if owner else {}

    credit_debt = db.execute(text(f"""
        SELECT COALESCE(SUM(ABS(ab.ledger)), 0)
        FROM accounts a LEFT JOIN account_balances ab ON ab.account_id = a.id
        WHERE a.type = 'credit' AND a.status = 'open' {bal_filter}
    """), bp).scalar()

    cash = db.execute(text(f"""
        SELECT COALESCE(SUM(COALESCE(ab.available, ab.ledger)), 0)
        FROM accounts a LEFT JOIN account_balances ab ON ab.account_id = a.id
        WHERE a.type = 'depository' AND a.subtype = 'checking'
          AND a.is_bills_only = FALSE AND a.status = 'open' {bal_filter}
    """), bp).scalar()

    savings_bal = db.execute(text(f"""
        SELECT COALESCE(SUM(COALESCE(ab.available, ab.ledger)), 0)
        FROM accounts a LEFT JOIN account_balances ab ON ab.account_id = a.id
        WHERE a.is_savings = TRUE AND a.exclude_from_savings = FALSE
          AND a.status = 'open' {bal_filter}
    """), bp).scalar()

    uncat = db.execute(text(f"""
        SELECT COUNT(*)
        FROM transactions t JOIN accounts a ON a.id = t.account_id
        WHERE t.cat_l2 IS NULL
          AND t.user_verified = FALSE
          AND t.status = 'posted'
          AND t.date BETWEEN :date_from AND :date_to {owner_filter}
    """), params).scalar()

    return {
        "period": period, "year": year, "month": month,
        "date_from": date_from, "date_to": date_to,
        "income":              float(income or 0),
        "spending":            float(spending or 0),
        "savings_in":          float(savings_in or 0),
        "savings_out":         float(savings_out or 0),
        "investment_in":       float(inv_in or 0),
        "investment_out":      float(inv_out or 0),
        "subscription_total":  float(sub_row[0] or 0),
        "subscription_count":  int(sub_row[1] or 0),
        "top_subscription":    sub_row[3],
        "top_subscription_amt": float(sub_row[2] or 0),
        "credit_debt":         float(credit_debt or 0),
        "cash_on_hand":        float(cash or 0),
        "savings_balance":     float(savings_bal or 0),
        "uncategorized_count": int(uncat or 0),
        "owner_filter":        owner,
    }


@app.get("/api/summary/by-category")
def get_spending_by_category(
    period: str = Query(default="month", regex="^(month|year)$"),
    year:   int = Query(default=None),
    month:  Optional[int] = Query(default=None, ge=1, le=12),
    owner:  Optional[str] = None,
    db: Session = Depends(get_db),
):
    today = date.today()
    if year is None: year = today.year
    if period == "month" and month is None: month = today.month
    date_from, date_to = _period_bounds(period, year, month)

    owner_filter = "AND a.owner = :owner" if owner else ""
    params: dict = {"date_from": date_from, "date_to": date_to}
    if owner: params["owner"] = owner

    rows = db.execute(text(f"""
        SELECT
          COALESCE(t.cat_l2, 'uncategorized')                          as cat_l2_id,
          COALESCE(c2.name, 'Uncategorized')                           as cat_l2_name,
          COALESCE(c2.color, c1.color, '#3d5268')                      as color,
          COALESCE(t.cat_l3, '')                                       as cat_l3_id,
          COALESCE(c3.name, '')                                        as cat_l3_name,
          COALESCE(t.cat_l4, '')                                       as cat_l4_id,
          COALESCE(c4.name, '')                                        as cat_l4_name,
          COUNT(*)                                                      as txn_count,
          SUM(ABS(t.amount))::numeric(12,2)                            as total,
          COALESCE(c2.budget_amount, 0)                                as budget
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        LEFT JOIN categories c1 ON c1.id = t.cat_l1
        LEFT JOIN categories c2 ON c2.id = t.cat_l2
        LEFT JOIN categories c3 ON c3.id = t.cat_l3
        LEFT JOIN categories c4 ON c4.id = t.cat_l4
        WHERE t.txn_class IN ('expense', 'subscription')
          AND t.amount < 0
          AND t.date BETWEEN :date_from AND :date_to
          AND t.status != 'pending'
          {owner_filter}
        GROUP BY t.cat_l2, c2.name, c2.color, c1.color,
                 t.cat_l3, c3.name, t.cat_l4, c4.name, c2.budget_amount
        ORDER BY total DESC
    """), params).fetchall()

    return [
        {
            "l1_id":    row[0], "l1_name": row[1], "color":    row[2],
            "l2_id":    row[3], "l2_name": row[4],
            "l3_id":    row[5], "l3_name": row[6],
            "txn_count": row[7], "total": float(row[8]),
            "budget":   float(row[9]) if row[9] else None,
        }
        for row in rows
    ]


@app.get("/api/summary/monthly-trend")
def get_monthly_trend(
    months: int = Query(default=12, ge=2, le=24),
    db: Session = Depends(get_db),
):
    rows = db.execute(text("""
        SELECT
          TO_CHAR(DATE_TRUNC('month', date), 'Mon YY')           as label,
          DATE_TRUNC('month', date)                              as month_date,
          SUM(CASE WHEN txn_class = 'income' AND amount > 0
                   THEN amount ELSE 0 END)::numeric(12,2)        as income,
          SUM(CASE WHEN txn_class IN ('expense','subscription') AND amount < 0
                   THEN ABS(amount) ELSE 0 END)::numeric(12,2)  as spending,
          SUM(CASE WHEN txn_class = 'investment_out'
                   THEN ABS(amount) ELSE 0 END)::numeric(12,2)  as investments,
          SUM(CASE WHEN txn_class = 'savings_in'
                   THEN ABS(amount) ELSE 0 END)::numeric(12,2)        as savings
        FROM transactions
        WHERE status != 'pending'
          AND date >= (CURRENT_DATE - INTERVAL '1 month' * :months)
        GROUP BY DATE_TRUNC('month', date)
        ORDER BY DATE_TRUNC('month', date) ASC
    """), {"months": months}).fetchall()

    return [{"label": r[0], "income": float(r[2]), "spending": float(r[3]),
             "investments": float(r[4]), "savings": float(r[5])} for r in rows]


# ============================================================
# CATEGORIES
# ============================================================

@app.get("/api/categories")
def get_categories(db: Session = Depends(get_db)):
    cats = db.query(Category).order_by(
        Category.level.asc(), Category.sort_order.asc(), Category.name.asc()
    ).all()
    return [_serialize_category(c) for c in cats]


@app.post("/api/categories")
def create_category(body: CategoryCreate, db: Session = Depends(get_db)):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name required")
    if body.parent_id:
        parent = db.get(Category, body.parent_id)
        if not parent:
            raise HTTPException(status_code=400, detail="Parent not found")
        level = parent.level + 1
    else:
        level = 1
    slug = re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')
    base = slug; i = 2
    while db.get(Category, slug):
        slug = f"{base}_{i}"; i += 1
    cat = Category(id=slug, name=name, parent_id=body.parent_id,
                   level=level, color=body.color,
                   budget_amount=body.budget_amount, sort_order=body.sort_order,
                   is_system=False)
    db.add(cat); db.commit()
    return _serialize_category(cat)


@app.patch("/api/categories/{cat_id}")
def update_category(cat_id: str, body: CategoryUpdate, db: Session = Depends(get_db)):
    cat = db.get(Category, cat_id)
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")
    if body.name is not None:          cat.name = body.name
    if body.color is not None:         cat.color = body.color
    if body.budget_amount is not None: cat.budget_amount = body.budget_amount
    if body.sort_order is not None:    cat.sort_order = body.sort_order
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
    if children:
        raise HTTPException(status_code=400, detail=f"Has {children} subcategories — delete those first")
    for col in ["cat_l1", "cat_l2", "cat_l3", "cat_l4"]:
        db.execute(text(f"UPDATE transactions SET {col} = NULL WHERE {col} = :id"), {"id": cat_id})
    db.delete(cat); db.commit()
    return {"ok": True}


# ============================================================
# DYNAMIC ENTRIES
# ============================================================

@app.get("/api/dynamic-entries")
def get_dynamic_entries(entry_type: Optional[str] = None, db: Session = Depends(get_db)):
    q = db.query(DynamicEntry).filter(DynamicEntry.is_active == True)
    if entry_type: q = q.filter(DynamicEntry.entry_type == entry_type)
    return [{"id": e.id, "entry_type": e.entry_type, "name": e.name,
             "description": e.description, "is_active": e.is_active, "sort_order": e.sort_order}
            for e in q.order_by(DynamicEntry.sort_order.asc(), DynamicEntry.name.asc()).all()]


@app.post("/api/dynamic-entries")
def create_dynamic_entry(body: DynamicEntryCreate, db: Session = Depends(get_db)):
    if body.entry_type not in VALID_ENTRY_TYPES:
        raise HTTPException(status_code=400, detail=f"entry_type must be one of {VALID_ENTRY_TYPES}")
    slug = body.entry_type + "_" + re.sub(r'[^a-z0-9]+', '_', body.name.lower()).strip('_')
    base = slug; i = 2
    while db.get(DynamicEntry, slug):
        slug = f"{base}_{i}"; i += 1
    entry = DynamicEntry(id=slug, entry_type=body.entry_type, name=body.name,
                         description=body.description, sort_order=body.sort_order)
    db.add(entry); db.commit()
    return {"id": entry.id, "entry_type": entry.entry_type, "name": entry.name,
            "description": entry.description, "is_active": entry.is_active, "sort_order": entry.sort_order}


@app.patch("/api/dynamic-entries/{entry_id}")
def update_dynamic_entry(entry_id: str, body: DynamicEntryUpdate, db: Session = Depends(get_db)):
    entry = db.get(DynamicEntry, entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    if body.name is not None:        entry.name = body.name
    if body.description is not None: entry.description = body.description
    if body.is_active is not None:   entry.is_active = body.is_active
    if body.sort_order is not None:  entry.sort_order = body.sort_order
    db.commit()
    return {"id": entry.id, "entry_type": entry.entry_type, "name": entry.name,
            "description": entry.description, "is_active": entry.is_active, "sort_order": entry.sort_order}


@app.delete("/api/dynamic-entries/{entry_id}")
def delete_dynamic_entry(entry_id: str, db: Session = Depends(get_db)):
    entry = db.get(DynamicEntry, entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    entry.is_active = False; db.commit()
    return {"ok": True}


# ============================================================
# MERCHANT RULES
# ============================================================

@app.get("/api/rules")
def get_rules(db: Session = Depends(get_db)):
    rules = db.query(MerchantRule).order_by(
        MerchantRule.priority.asc(), MerchantRule.match_count.desc()
    ).all()
    return [_serialize_rule(r) for r in rules]


@app.post("/api/rules")
def create_rule(body: RuleCreate, db: Session = Depends(get_db)):
    val = body.match_value.strip()
    if not val:
        raise HTTPException(status_code=400, detail="match_value required")
    if body.match_type not in VALID_MATCH_TYPES:
        raise HTTPException(status_code=400, detail="Invalid match_type")
    if body.txn_class and body.txn_class not in VALID_TXN_CLASSES:
        raise HTTPException(status_code=400, detail="Invalid txn_class")

    existing = db.query(MerchantRule).filter(
        MerchantRule.match_type == body.match_type,
        MerchantRule.match_value.ilike(val),
        MerchantRule.is_active == True,
    ).first()

    if existing:
        if body.txn_class:      existing.txn_class = body.txn_class
        if body.cat_l1:         existing.cat_l1 = body.cat_l1
        if body.cat_l2:         existing.cat_l2 = body.cat_l2
        if body.cat_l3:         existing.cat_l3 = body.cat_l3
        if body.cat_l4:         existing.cat_l4 = body.cat_l4
        if body.recurring_type: existing.recurring_type = body.recurring_type
        if body.merchant_clean: existing.merchant_clean = body.merchant_clean
        existing.is_active = True; db.commit()
        if body.apply_to_existing: _apply_rule_to_existing(db, existing)
        return {**_serialize_rule(existing), "was_duplicate": True}

    slug = "rule_" + re.sub(r'[^a-z0-9]+', '_', val.lower())[:40]
    base = slug; i = 2
    while db.get(MerchantRule, slug):
        slug = f"{base}_{i}"; i += 1

    cat_l1 = body.cat_l1 or (TXN_CLASS_TO_CAT_L1.get(body.txn_class) if body.txn_class else None)

    rule = MerchantRule(
        id=slug, match_type=body.match_type, match_value=val,
        txn_class=body.txn_class, cat_l1=cat_l1,
        cat_l2=body.cat_l2, cat_l3=body.cat_l3, cat_l4=body.cat_l4,
        recurring_type=body.recurring_type, merchant_clean=body.merchant_clean,
        priority=body.priority, is_system=False, is_active=True,
    )
    db.add(rule); db.commit()
    if body.apply_to_existing: _apply_rule_to_existing(db, rule)
    return _serialize_rule(rule)


@app.patch("/api/rules/{rule_id}")
def update_rule(rule_id: str, body: RuleUpdate, db: Session = Depends(get_db)):
    rule = db.get(MerchantRule, rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    if body.match_type is not None:     rule.match_type = body.match_type
    if body.match_value is not None:    rule.match_value = body.match_value
    if body.txn_class is not None:
        rule.txn_class = body.txn_class
        if body.txn_class and not body.cat_l1:
            rule.cat_l1 = TXN_CLASS_TO_CAT_L1.get(body.txn_class, rule.cat_l1)
    if body.cat_l1 is not None:         rule.cat_l1 = body.cat_l1
    if body.cat_l2 is not None:         rule.cat_l2 = body.cat_l2
    if body.cat_l3 is not None:         rule.cat_l3 = body.cat_l3
    if body.cat_l4 is not None:         rule.cat_l4 = body.cat_l4
    if body.recurring_type is not None: rule.recurring_type = body.recurring_type
    if body.merchant_clean is not None: rule.merchant_clean = body.merchant_clean
    if body.priority is not None:       rule.priority = body.priority
    if body.is_active is not None:      rule.is_active = body.is_active
    db.commit()
    if body.apply_to_existing: _apply_rule_to_existing(db, rule)
    return _serialize_rule(rule)


@app.delete("/api/rules/{rule_id}")
def delete_rule(rule_id: str, db: Session = Depends(get_db)):
    rule = db.get(MerchantRule, rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    if rule.is_system:
        raise HTTPException(status_code=400, detail="Cannot delete system rules — disable instead")
    db.delete(rule); db.commit()
    return {"ok": True}


# ============================================================
# BALANCES
# ============================================================

@app.post("/api/balances/refresh")
def refresh_balances(db: Session = Depends(get_db)):
    from app.services.teller_client import TellerClient
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
                existing.ledger    = Decimal(str(bal.get("ledger", 0) or 0))
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


# ============================================================
# SCHEDULER — V4.1.1: hourly instead of daily
# ============================================================

scheduler = BackgroundScheduler()

def _scheduled_sync():
    db = SessionLocal()
    try:
        sync_all(db)
    finally:
        db.close()

# Run every hour on the hour
scheduler.add_job(_scheduled_sync, "interval", hours=1)
scheduler.start()


# ============================================================
# FRONTEND
# ============================================================

@app.get("/")
@app.get("/ui")
def serve_frontend():
    if os.path.exists(FRONTEND_PATH):
        return FileResponse(FRONTEND_PATH)
    return {"error": "Frontend not found"}