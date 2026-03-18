# Budget Duo

A self-hosted personal finance and budgeting application for the Honey Duo household. Built on top of the [Teller](https://teller.io) banking API for real-time transaction data across all linked bank accounts.

**Live at:** https://budget.honey-duo.com (Cloudflare Access protected)  
**Internal:** http://192.168.0.245:8500  
**Server:** Ubuntu RTX 3090 (192.168.0.245)  
**Stack:** FastAPI + PostgreSQL + React (htm/CDN, no build step)

---

## What It Does

- Connects to all household bank accounts via Teller (Bank of America, Capital One, Citi, Amex)
- Automatically classifies every transaction — separating real spending from transfers, CC payments, investments, and savings moves
- Two-tier category system with optional budget targets per category
- Merchant rules engine that learns your patterns and auto-classifies new transactions going forward
- Live account balances across all checking, savings, and credit cards
- Subscription and utility tracking with cancel/active status
- Household split visibility — Sam, Jess, and joint account filtering

---

## Infrastructure

### Docker Services

```
budget-duo-backend    FastAPI app on port 8500
budget-duo-db         PostgreSQL 16 on port 5432
```

### Data Storage

```
/mnt/storage/docker/budget-duo/postgres    PostgreSQL data (persistent volume)
/home/honey-duo/.teller/certs              Teller mTLS certificates (read-only mount)
~/Budget-Duo/backend/static/index.html    Frontend served by FastAPI
```

### Cloudflare Integration

Routed through the Ubuntu Cloudflare tunnel (`2f0be609-2dee-4e1a-be2f-c8f83648421e`):

```
budget.honey-duo.com → localhost:8500
```

Protected by Cloudflare Access with email OTP authentication. Only authorized household emails can access.

### Ports

| Port | Service |
|------|---------|
| 8500 | FastAPI backend + frontend |
| 5432 | PostgreSQL (internal only) |

---

## Service Management

```bash
cd ~/Budget-Duo

# Status
docker compose ps

# Restart backend
docker compose restart backend

# View logs
docker compose logs backend --tail=50

# Full restart
docker compose down && docker compose up -d
```

---

## Frontend Deployment

The frontend is a single `index.html` file served as a static asset by FastAPI. To deploy updates:

```bash
# Copy new file to static folder
cp /path/to/new/index.html ~/Budget-Duo/backend/static/index.html

# Copy into running container (no restart needed)
docker cp ~/Budget-Duo/backend/static/index.html budget-duo-backend:/app/static/index.html
```

**Planned:** Add volume mount to docker-compose.yml so file changes take effect without docker cp:
```yaml
- /home/honey-duo/Budget-Duo/backend/static:/app/static
```

---

## Backend API

All endpoints served at `http://192.168.0.245:8500`:

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/accounts` | All accounts with live balances |
| GET | `/api/transactions` | Transactions with filtering |
| PATCH | `/api/transactions/:id` | Update class, category, recurring type |
| GET | `/api/categories` | Full two-tier category tree |
| POST | `/api/categories` | Create category or subcategory |
| PATCH | `/api/categories/:id` | Edit category or set budget |
| DELETE | `/api/categories/:id` | Delete user category |
| GET | `/api/rules` | All merchant classification rules |
| POST | `/api/rules` | Create new merchant rule |
| PATCH | `/api/rules/:id` | Edit or toggle rule |
| DELETE | `/api/rules/:id` | Delete user rule |
| GET | `/api/summary` | Dashboard summary (MTD spending, income, balances, debt) |
| GET | `/api/summary/by-category` | Spending breakdown by category |
| GET | `/api/summary/monthly` | Month-over-month history |
| GET | `/api/recurring` | Subscriptions and recurring charges |
| POST | `/api/balances/refresh` | Pull fresh balances from Teller |
| POST | `/api/sync` | Sync all accounts (last 10 days) |
| POST | `/api/classify` | Re-run classifier on all transactions |

---

## Database

PostgreSQL 16. Key tables:

| Table | Purpose |
|-------|---------|
| `transactions` | All transactions with classification fields |
| `accounts` | Linked bank accounts and metadata |
| `enrollments` | Teller enrollment tokens per institution |
| `categories` | Two-tier category system with budget targets |
| `merchant_rules` | Auto-classification rules engine |
| `account_balances` | Cached live balances from Teller |
| `sync_log` | History of all sync operations |

### Useful Queries

```bash
# Connect
docker compose exec db psql -U budget_duo -d budget_duo

# Real spending this month
SELECT SUM(ABS(amount)) FROM transactions
WHERE txn_class='expense' AND amount<0
AND date >= DATE_TRUNC('month', CURRENT_DATE);

# Classification breakdown
SELECT txn_class, COUNT(*), SUM(ABS(amount))::numeric(12,2)
FROM transactions GROUP BY txn_class ORDER BY count DESC;

# Check merchant rules firing
SELECT id, match_value, txn_class, match_count
FROM merchant_rules WHERE match_count > 0
ORDER BY match_count DESC LIMIT 20;
```

---

## Transaction Classification

Every transaction gets a `txn_class` that determines whether it counts as spending:

| Class | Meaning | Counts as spending? |
|-------|---------|-------------------|
| `expense` | Real purchase | Yes |
| `income` | Payroll, deposits | No |
| `internal_transfer` | Between own accounts | No |
| `cc_payment` | Paying a credit card | No |
| `investment_out` | Fidelity/Roth IRA | No |
| `investment_in` | ETrade liquidation | No |
| `debt_payment` | Affirm installments | No |
| `savings_move` | Checking to savings | No |
| `ignore` | Interest charges, fees | No |

Classification runs automatically on every sync. The Merchant Rules engine applies user-defined and system rules first, then falls back to Teller's txn_type field. Manual overrides are never overwritten by the classifier.

---

## Teller Integration

Budget Duo uses Teller's real banking API (not screen-scraping). Connected institutions:

- Bank of America (Jess — checking, savings, credit)
- Bank of America (Sam — checking, savings)
- Capital One (Sam and Jess)
- Citibank (Sam)
- American Express (Sam)

Teller certificates live at `/home/honey-duo/.teller/certs/` and are mounted read-only into the container. Tokens are stored in `.env` (gitignored).

Sync runs automatically daily at 6am and can be triggered manually from the UI sync button.

---

## Environment Variables

Stored in `~/Budget-Duo/.env` (gitignored). See `.env.example` for required variables:

```
POSTGRES_PASSWORD=
SECRET_KEY=
TOKEN_JESS_BOFA=
TOKEN_JESS_CAPONE=
TOKEN_SAM_BOFA=
TOKEN_SAM_AMEX=
TOKEN_SAM_CAPONE=
TOKEN_SAM_CITI=
```

Credentials stored in Vaultwarden under Infrastructure collection.

---

## Backups

PostgreSQL data is stored at `/mnt/storage/docker/budget-duo/postgres`. Should be included in Ubuntu backup strategy.

```bash
# Manual backup
docker compose exec db pg_dump -U budget_duo budget_duo > ~/backups/budget-duo-$(date +%Y%m%d).sql
```

---

## Development Notes

### Re-running Classification

After adding new merchant rules, reclassify all existing transactions:

```bash
docker compose exec backend python -m app.services.classify_backfill
```

### Database Migrations

V2 migration script is at `backend/migrate_v2.sql`. For future changes:

```bash
docker compose exec -T db psql -U budget_duo -d budget_duo < backend/migrate_vN.sql
```

---

## Monitoring Integration (Planned)

- Uptime Kuma: HTTP check on https://budget.honey-duo.com
- Prometheus: /metrics endpoint from FastAPI
- Grafana: Monthly spending trends, sync health dashboard
- Loki: FastAPI and PostgreSQL logs via Promtail

---

## Known Issues / Roadmap

- [ ] Volume mount for frontend (avoid docker cp on every deploy)
- [ ] Cloudflare Access OTP gate (pending DNS CNAME setup)
- [ ] Budget progress bars per category (backend done, UI pending)
- [ ] Month-over-month trend charts
- [ ] Uptime Kuma monitoring
- [ ] Automated PostgreSQL backups to OneDrive
- [ ] Teller webhooks for real-time sync

---

**Last Updated:** March 17, 2026  
**Maintained by:** Sam  
**Related:** https://github.com/HoneyDuoDevelopments/honey-duo-infrastructure