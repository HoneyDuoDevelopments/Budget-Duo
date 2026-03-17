from sqlalchemy import (
    Column, String, Numeric, Date, DateTime, Boolean,
    Text, ForeignKey, UniqueConstraint, Index
)
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.sql import func

Base = declarative_base()


class Enrollment(Base):
    __tablename__ = "enrollments"

    id = Column(String, primary_key=True)  # Teller enrollment_id
    institution_id = Column(String, nullable=False)
    institution_name = Column(String, nullable=False)
    owner = Column(String, nullable=False)  # "sam" | "jess" | "joint"
    access_token = Column(String, nullable=False)
    status = Column(String, default="active")  # active | disconnected
    connected_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, onupdate=func.now())

    accounts = relationship("Account", back_populates="enrollment")


class Account(Base):
    __tablename__ = "accounts"

    id = Column(String, primary_key=True)  # Teller account_id (canonical)
    enrollment_id = Column(String, ForeignKey("enrollments.id"), nullable=False)
    alias_id = Column(String, nullable=True)  # Non-canonical duplicate ID if any
    institution_name = Column(String, nullable=False)
    name = Column(String, nullable=False)
    type = Column(String, nullable=False)       # depository | credit
    subtype = Column(String, nullable=False)    # checking | savings | credit_card
    last_four = Column(String, nullable=False)
    currency = Column(String, default="USD")
    owner = Column(String, nullable=False)      # sam | jess | joint
    role = Column(String, nullable=False)       # household_checking | personal_checking
                                                # household_savings_bills | personal_savings
                                                # credit
    is_bills_only = Column(Boolean, default=False)
    is_savings = Column(Boolean, default=False)
    status = Column(String, default="open")
    created_at = Column(DateTime, server_default=func.now())

    enrollment = relationship("Enrollment", back_populates="accounts")
    transactions = relationship("Transaction", back_populates="account")


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(String, primary_key=True)           # Teller txn_id
    account_id = Column(String, ForeignKey("accounts.id"), nullable=False)
    amount = Column(Numeric(12, 2), nullable=False)  # Negative = debit, positive = credit
    date = Column(Date, nullable=False)
    description = Column(String, nullable=False)     # Raw bank description
    counterparty_name = Column(String, nullable=True)
    counterparty_type = Column(String, nullable=True)
    teller_category = Column(String, nullable=True)  # Teller's enriched category
    custom_category = Column(String, nullable=True)  # User assigned
    txn_type = Column(String, nullable=True)         # card_payment | transfer | atm etc
    status = Column(String, nullable=False)          # posted | pending
    is_income = Column(Boolean, default=False)
    is_recurring = Column(Boolean, default=False)
    recurring_group = Column(String, nullable=True)  # Groups recurring txns by merchant
    raw_json = Column(Text, nullable=True)           # Full Teller payload
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    account = relationship("Account", back_populates="transactions")

    __table_args__ = (
        Index("ix_transactions_account_date", "account_id", "date"),
        Index("ix_transactions_date", "date"),
        Index("ix_transactions_status", "status"),
        Index("ix_transactions_recurring", "is_recurring"),
    )


class Category(Base):
    __tablename__ = "categories"

    id = Column(String, primary_key=True)  # slug e.g. "groceries"
    name = Column(String, nullable=False)   # Display name e.g. "Groceries"
    parent_id = Column(String, ForeignKey("categories.id"), nullable=True)
    color = Column(String, nullable=True)   # Hex color for UI
    icon = Column(String, nullable=True)
    is_system = Column(Boolean, default=False)  # Teller built-in vs user created
    created_at = Column(DateTime, server_default=func.now())


class SyncLog(Base):
    __tablename__ = "sync_log"

    id = Column(String, primary_key=True)
    account_id = Column(String, ForeignKey("accounts.id"), nullable=False)
    sync_type = Column(String, nullable=False)  # webhook | scheduled | manual
    status = Column(String, nullable=False)     # success | failed | partial
    txns_added = Column(String, default="0")
    txns_updated = Column(String, default="0")
    error_message = Column(Text, nullable=True)
    synced_at = Column(DateTime, server_default=func.now())