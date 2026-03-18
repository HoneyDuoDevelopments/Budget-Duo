"""
V2 Classification Backfill
Run with: docker compose exec backend python -m app.services.classify_backfill
"""
from app.db.session import SessionLocal
from app.services.classifier import classify_all


def run():
    print("Starting V2 classification backfill...")
    db = SessionLocal()
    try:
        result = classify_all(db, only_unclassified=False)
        print(f"\n✅ Backfill complete:")
        print(f"   Total processed:  {result['total']}")
        print(f"   Rule matched:     {result['rule_matched']}")
        print(f"   Fallback used:    {result['fallback_used']}")
        print(f"\nRun the audit query to verify numbers:")
        print("""
docker compose exec db psql -U budget_duo -d budget_duo -c "
SELECT txn_class, COUNT(*) as count, SUM(ABS(amount))::numeric(12,2) as volume
FROM transactions
GROUP BY txn_class
ORDER BY count DESC;
"
        """)
    finally:
        db.close()


if __name__ == "__main__":
    run()
