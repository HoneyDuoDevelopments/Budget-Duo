"""
Budget Duo — DB Models V4
"""
from sqlalchemy import (
    Column, String, Numeric, Date, DateTime, Boolean,
    Text, ForeignKey, Index, Integer, SmallInteger
)
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.sql import func

Base = declarative_base()


class Enrollment(Base):
    __tablename__ = "enrollments"

    id               = Column(String, primary_key=True)
    institution_id   = Column(String, nullable=False)
    institution_name = Column(String, nullable=False)
    owner            = Column(String, nullable=False)   # sam | jess | joint
    access_token     = Column(String, nullable=False)
    status           = Column(String, default="active") # active | disconnected
    connected_at     = Column(DateTime, server_default=func.now())
    updated_at       = Column(DateTime, onupdate=func.now())

    accounts = relationship("Account", back_populates="enrollment")


class Account(Base):
    __tablename__ = "accounts"

    id                   = Column(String, primary_key=True)
    enrollment_id        = Column(String, ForeignKey("enrollments.id"), nullable=False)
    alias_id             = Column(String, nullable=True)
    institution_name     = Column(String, nullable=False)
    name                 = Column(String, nullable=False)
    type                 = Column(String, nullable=False)    # depository | credit
    subtype              = Column(String, nullable=False)    # checking | savings | credit_card
    last_four            = Column(String, nullable=False)
    currency             = Column(String, default="USD")
    owner                = Column(String, nullable=False)    # sam | jess | joint
    role                 = Column(String, nullable=False)
    is_bills_only        = Column(Boolean, default=False)
    is_savings           = Column(Boolean, default=False)
    exclude_from_savings = Column(Boolean, default=False)   # V4: 0687 mortgage/truck account
    status               = Column(String, default="open")
    created_at           = Column(DateTime, server_default=func.now())

    enrollment   = relationship("Enrollment", back_populates="accounts")
    transactions = relationship("Transaction", back_populates="account")
    balance      = relationship("AccountBalance", back_populates="account", uselist=False)


class Transaction(Base):
    __tablename__ = "transactions"

    id                = Column(String, primary_key=True)
    account_id        = Column(String, ForeignKey("accounts.id"), nullable=False)
    amount            = Column(Numeric(12, 2), nullable=False)
    date              = Column(Date, nullable=False)
    description       = Column(String, nullable=False)
    counterparty_name = Column(String, nullable=True)
    counterparty_type = Column(String, nullable=True)
    teller_category   = Column(String, nullable=True)

    # Classification
    txn_class      = Column(String(32), nullable=True)   # income | expense | savings_in | savings_out | investment_in | investment_out | subscription | internal_transfer | cc_payment | ignore
    merchant_clean = Column(String(255), nullable=True)
    rule_id        = Column(String(255), nullable=True)
    user_verified  = Column(Boolean, default=False)

    # V4: 4-level category tree
    cat_l1 = Column(String(255), ForeignKey("categories.id", ondelete="SET NULL"), nullable=True)
    cat_l2 = Column(String(255), ForeignKey("categories.id", ondelete="SET NULL"), nullable=True)
    cat_l3 = Column(String(255), ForeignKey("categories.id", ondelete="SET NULL"), nullable=True)
    cat_l4 = Column(String(255), ForeignKey("categories.id", ondelete="SET NULL"), nullable=True)
    cat_l5 = Column(String(255), ForeignKey("dynamic_entries.id", ondelete="SET NULL"), nullable=True)

    # Kept for compat / subscription detection
    recurring_type = Column(String(32), nullable=True)
    is_recurring   = Column(Boolean, default=False)

    # Legacy — kept so existing data isn't lost, not used in V4 UI
    category_id    = Column(String, nullable=True)
    subcategory_id = Column(String, nullable=True)
    custom_category = Column(String, nullable=True)
    txn_type       = Column(String, nullable=True)
    status         = Column(String, nullable=False)
    is_income      = Column(Boolean, default=False)
    recurring_group = Column(String, nullable=True)
    raw_json       = Column(Text, nullable=True)
    created_at     = Column(DateTime, server_default=func.now())
    updated_at     = Column(DateTime, server_default=func.now(), onupdate=func.now())
    import_source  = Column(String(32), default="teller")   
    account   = relationship("Account", back_populates="transactions")
    l1_cat    = relationship("Category", foreign_keys=[cat_l1])
    l2_cat    = relationship("Category", foreign_keys=[cat_l2])
    l3_cat    = relationship("Category", foreign_keys=[cat_l3])
    l4_cat    = relationship("Category", foreign_keys=[cat_l4])
    l5_entry  = relationship("DynamicEntry", foreign_keys=[cat_l5])

    __table_args__ = (
        Index("ix_transactions_account_date",  "account_id", "date"),
        Index("ix_transactions_date",          "date"),
        Index("ix_transactions_status",        "status"),
        Index("ix_transactions_recurring",     "is_recurring"),
        Index("ix_transactions_txn_class",     "txn_class"),
        Index("ix_transactions_cat_l1",        "cat_l1"),
        Index("ix_transactions_cat_l2",        "cat_l2"),
        Index("ix_transactions_cat_l3",        "cat_l3"),
        Index("ix_transactions_cat_l4",        "cat_l4"),
        Index("ix_transactions_cat_l5",        "cat_l5"),
        Index("ix_transactions_merchant_clean","merchant_clean"),
    )


class Category(Base):
    __tablename__ = "categories"

    id                = Column(String, primary_key=True)
    name              = Column(String, nullable=False)
    parent_id         = Column(String, ForeignKey("categories.id"), nullable=True)
    level             = Column(SmallInteger, default=1)      # 1 | 2 | 3 | 4
    color             = Column(String, nullable=True)
    icon              = Column(String, nullable=True)
    is_system         = Column(Boolean, default=False)
    is_dynamic_parent = Column(Boolean, default=False)       # has dynamic L4 children from dynamic_entries
    entry_type        = Column(String(32), nullable=True)    # 'car' | 'project' | 'trip'
    budget_amount     = Column(Numeric(12, 2), nullable=True)
    sort_order        = Column(Integer, default=50)
    created_at        = Column(DateTime, server_default=func.now())

    parent   = relationship("Category", remote_side=[id], foreign_keys=[parent_id])
    children = relationship("Category", foreign_keys=[parent_id])


class DynamicEntry(Base):
    """Cars, home projects, trips — used as L4 category options."""
    __tablename__ = "dynamic_entries"

    id          = Column(String(255), primary_key=True)
    entry_type  = Column(String(32), nullable=False)    # 'car' | 'project' | 'trip'
    name        = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    is_active   = Column(Boolean, default=True)
    sort_order  = Column(Integer, default=50)
    created_at  = Column(DateTime, server_default=func.now())


class MerchantRule(Base):
    __tablename__ = "merchant_rules"

    id             = Column(String(255), primary_key=True)
    match_type     = Column(String(32), nullable=False)
    match_value    = Column(String(512), nullable=False)
    txn_class      = Column(String(32), nullable=True)
    cat_l1         = Column(String(255), ForeignKey("categories.id", ondelete="SET NULL"), nullable=True)
    cat_l2         = Column(String(255), ForeignKey("categories.id", ondelete="SET NULL"), nullable=True)
    cat_l3         = Column(String(255), ForeignKey("categories.id", ondelete="SET NULL"), nullable=True)
    cat_l4         = Column(String(255), ForeignKey("categories.id", ondelete="SET NULL"), nullable=True)
    recurring_type = Column(String(32), nullable=True)
    merchant_clean = Column(String(255), nullable=True)
    priority       = Column(Integer, default=100)
    is_system      = Column(Boolean, default=False)
    is_active      = Column(Boolean, default=True)
    match_count    = Column(Integer, default=0)
    created_at     = Column(DateTime, server_default=func.now())
    updated_at     = Column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_merchant_rules_match_type", "match_type"),
        Index("ix_merchant_rules_active",     "is_active"),
    )


class AccountBalance(Base):
    __tablename__ = "account_balances"

    account_id = Column(String, ForeignKey("accounts.id"), primary_key=True)
    ledger     = Column(Numeric(12, 2), nullable=True)
    available  = Column(Numeric(12, 2), nullable=True)
    fetched_at = Column(DateTime(timezone=True), server_default=func.now())

    account = relationship("Account", back_populates="balance")


class SyncLog(Base):
    __tablename__ = "sync_log"

    id            = Column(String, primary_key=True)
    account_id    = Column(String, ForeignKey("accounts.id"), nullable=False)
    sync_type     = Column(String, nullable=False)
    status        = Column(String, nullable=False)
    txns_added    = Column(Integer, default=0)
    txns_updated  = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)
    synced_at     = Column(DateTime, server_default=func.now())