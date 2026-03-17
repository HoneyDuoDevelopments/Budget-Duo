"""
One-time enrollment seeder.
Run with: python -m app.services.enrollment_seed
Reads access tokens from environment variables.
"""
import os
import uuid
from app.db.session import SessionLocal
from app.db.models import Enrollment, Account
from app.services.teller_client import TellerClient
from app.config import settings

# Account metadata we know from the API discovery phase
ACCOUNT_META = {
    # Canonical ID -> (owner, role, is_bills_only, is_savings)
    "acc_ppuaa3d9jaqul1qusa000": ("joint", "household_checking", False, False),
    "acc_ppuaa3d83aqul1qusa000": ("jess",  "personal_checking",  False, False),
    "acc_ppuaa3d73aqul1qusa000": ("joint", "household_savings_bills", True, False),
    "acc_ppuaa3d53aqul1qusa000": ("jess",  "personal_savings",   False, True),
    "acc_ppu17g8urisnfdkj2q002": ("sam",   "credit",             False, False),
    "acc_ppu17g8vbisnfdkj2q001": ("sam",   "credit",             False, False),
    "acc_ppu17g8urisnfdkj2q000": ("sam",   "personal_checking",  False, False),
    "acc_ppu17g8trisnfdkj2q001": ("sam",   "personal_savings",   False, True),
    "acc_ppu9s6n7qjcpji6aso000": ("sam",   "credit",             False, False),
    "acc_ppu9pf8g3aqul1qusa000": ("sam",   "credit",             False, False),
    "acc_ppu9pf8d3aqul1qusa000": ("sam",   "personal_checking",  False, False),
    "acc_ppu9pf8ejaqul1qusa000": ("sam",   "personal_savings",   False, True),
    "acc_ppu9mdmjqjcpji6aso000": ("sam",   "credit",             False, False),
    "acc_ppuagbi13aqul1qusa000": ("jess",  "credit",             False, False),
}

ENROLLMENTS = [
    {
        "id": "enr_ppuaa3dqkuov6dnsr4000",
        "institution_id": "bank_of_america",
        "institution_name": "Bank of America",
        "owner": "jess",
        "token_env": "TOKEN_JESS_BOFA",
    },
    {
        "id": "enr_ppu8llv34uov6dnsr4000",
        "institution_id": "bank_of_america",
        "institution_name": "Bank of America",
        "owner": "sam",
        "token_env": "TOKEN_SAM_BOFA",
    },
    {
        "id": "enr_ppu9s6nl4uov6dnsr4000",
        "institution_id": "amex",
        "institution_name": "American Express",
        "owner": "sam",
        "token_env": "TOKEN_SAM_AMEX",
    },
    {
        "id": "enr_ppu9pf95kuov6dnsr4000",
        "institution_id": "capital_one",
        "institution_name": "CapitalOne",
        "owner": "sam",
        "token_env": "TOKEN_SAM_CAPONE",
    },
    {
        "id": "enr_ppu9msrlkuov6dnsr4000",
        "institution_id": "citibank",
        "institution_name": "Citibank",
        "owner": "sam",
        "token_env": "TOKEN_SAM_CITI",
    },
    {
        "id": "enr_ppubm4fqkuov6dnsr4000",
        "institution_id": "capital_one",
        "institution_name": "CapitalOne",
        "owner": "jess",
        "token_env": "TOKEN_JESS_CAPONE",
    },
]


def seed():
    db = SessionLocal()
    try:
        for enr_data in ENROLLMENTS:
            token = os.environ.get(enr_data["token_env"])
            if not token:
                print(f"⚠️  Missing env var {enr_data['token_env']} — skipping")
                continue

            # Upsert enrollment
            enr = db.get(Enrollment, enr_data["id"])
            if not enr:
                enr = Enrollment(
                    id=enr_data["id"],
                    institution_id=enr_data["institution_id"],
                    institution_name=enr_data["institution_name"],
                    owner=enr_data["owner"],
                    access_token=token,
                    status="active",
                )
                db.add(enr)
                print(f"✅ Created enrollment: {enr_data['institution_name']} ({enr_data['owner']})")
            else:
                enr.access_token = token
                print(f"🔄 Updated token for: {enr_data['institution_name']} ({enr_data['owner']})")

            # Fetch and upsert accounts for this enrollment
            client = TellerClient(token)
            try:
                accounts = client.get_accounts()
            except Exception as e:
                print(f"❌ Failed to fetch accounts for {enr_data['id']}: {e}")
                continue

            for acct in accounts:
                acct_id = acct["id"]

                # Skip alias accounts — only store canonical IDs
                if acct_id in settings.ACCOUNT_ALIASES:
                    print(f"  ⏭️  Skipping alias account {acct_id} (last4: {acct['last_four']})")
                    continue

                meta = ACCOUNT_META.get(acct_id)
                if not meta:
                    print(f"  ⚠️  Unknown account {acct_id} — skipping")
                    continue

                owner, role, is_bills_only, is_savings = meta

                # Find alias ID for this canonical account if any
                alias_id = next(
                    (k for k, v in settings.ACCOUNT_ALIASES.items() if v == acct_id),
                    None
                )

                existing = db.get(Account, acct_id)
                if not existing:
                    db.add(Account(
                        id=acct_id,
                        enrollment_id=enr_data["id"],
                        alias_id=alias_id,
                        institution_name=acct["institution"]["name"],
                        name=acct["name"],
                        type=acct["type"],
                        subtype=acct["subtype"],
                        last_four=acct["last_four"],
                        currency=acct.get("currency", "USD"),
                        owner=owner,
                        role=role,
                        is_bills_only=is_bills_only,
                        is_savings=is_savings,
                        status=acct.get("status", "open"),
                    ))
                    print(f"  ✅ Account: {acct['name']} ({owner} / {role})")
                else:
                    print(f"  ⏭️  Account exists: {acct['name']}")

        db.commit()
        print("\n✅ Seed complete")
    except Exception as e:
        db.rollback()
        print(f"\n❌ Seed failed: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed()