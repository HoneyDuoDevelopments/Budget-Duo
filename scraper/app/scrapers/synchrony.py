"""
Synchrony Scraper — Playwright automation
Handles: login → balance scrape → transaction DOM scrape for all accounts

Two accounts:
- Discount Tire / Synchrony Car Care (ending 5339)
- Amazon Prime Store Card (ending 8814)

No 2FA required. Transactions are scraped from the Activity table DOM
since Synchrony has no CSV export.
"""
import asyncio
import logging
import os
import re
from datetime import datetime

from playwright.async_api import async_playwright, Page

logger = logging.getLogger(__name__)

SYNCHRONY_URL = "https://www.synchrony.com/accounts/"
LOGIN_URL = "https://www.synchrony.com/idp/en/signin"

# Map last4 to our internal account IDs
ACCOUNT_MAP = {
    "5339": {
        "account_id": "acc_scraper_sync_5339",
        "name": "Discount Tire / Synchrony Car Care",
    },
    "8814": {
        "account_id": "acc_scraper_sync_8814",
        "name": "Amazon Prime Store Card",
    },
}


async def scrape_synchrony(
    session: dict,
    username: str,
    password: str,
) -> dict:
    """
    Full Synchrony scrape: login, balance scrape, transaction DOM scrape.
    Updates session dict in-place for status polling.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        try:
            # ── STEP 1: Login ──
            session["status"] = "logging_in"
            logger.info("Navigating to Synchrony login")
            await page.goto(LOGIN_URL, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2000)

            # Fill username
            username_field = page.locator("input#username")
            await username_field.wait_for(state="visible", timeout=10000)
            await username_field.fill(username)

            # Fill password
            password_field = page.locator("input#password")
            await password_field.fill(password)
            await page.wait_for_timeout(500)

            # Click Sign In
            submit_btn = page.locator('button[type="submit"]')
            await submit_btn.click()
            await page.wait_for_timeout(5000)

            # Verify we landed on the accounts page
            try:
                await page.wait_for_url("**/accounts/**", timeout=20000)
            except Exception:
                # Check if there's a 2FA or security challenge
                content = await page.content()
                if "security" in content.lower() or "verify" in content.lower():
                    raise Exception("Synchrony requested security verification — manual intervention needed")
                if "incorrect" in content.lower() or "invalid" in content.lower():
                    raise Exception("Login failed — incorrect credentials")
                # Might have landed somewhere unexpected
                logger.warning(f"Unexpected URL after login: {page.url}")

            session["status"] = "authenticated"
            logger.info(f"Authenticated — on {page.url}")
            await page.wait_for_timeout(3000)

            # ── STEP 2: Scrape balances from dashboard ──
            session["status"] = "scraping_balance"
            accounts_data = await _scrape_dashboard_balances(page)
            session["accounts"] = accounts_data
            logger.info(f"Scraped balances for {len(accounts_data)} accounts")

            # ── STEP 3: Scrape transactions for each account ──
            session["status"] = "scraping_transactions"
            all_transactions = []

            for acct in accounts_data:
                last4 = acct.get("last_four", "")
                acct_info = ACCOUNT_MAP.get(last4)
                if not acct_info:
                    logger.warning(f"Unknown Synchrony account ending {last4} — skipping transactions")
                    continue

                logger.info(f"Scraping transactions for {acct_info['name']} (···{last4})")
                txns = await _scrape_account_transactions(page, acct, acct_info)
                all_transactions.extend(txns)
                logger.info(f"  Found {len(txns)} transactions")

                # Navigate back to dashboard for the next account
                await page.goto(SYNCHRONY_URL, wait_until="networkidle", timeout=20000)
                await page.wait_for_timeout(2000)

            session["transactions"] = all_transactions
            session["txn_count"] = len(all_transactions)
            session["status"] = "complete"

            logger.info(f"Synchrony scrape complete: {len(accounts_data)} accounts, {len(all_transactions)} transactions")

            return {
                "status": "complete",
                "accounts": accounts_data,
                "transactions": all_transactions,
                "txn_count": len(all_transactions),
            }

        except Exception as e:
            logger.exception(f"Synchrony scraper error: {e}")
            try:
                await page.screenshot(path="/tmp/synchrony_error.png")
            except Exception:
                pass
            session["status"] = "error"
            session["error"] = str(e)
            return {"status": "error", "error": str(e)}

        finally:
            await browser.close()


async def _scrape_dashboard_balances(page: Page) -> list[dict]:
    """
    Scrape balance data for all accounts on the Synchrony dashboard.
    Each card shows: account name, last 4, current balance, available to spend.
    Uses data-cy attributes for reliable selection.
    """
    accounts = []

    # Each account card is in a div with data-testid="desktop-card-container"
    cards = page.locator('[data-testid="desktop-card-container"]')
    card_count = await cards.count()
    logger.info(f"Found {card_count} account cards on dashboard")

    for i in range(card_count):
        card = cards.nth(i)
        acct = {"last_four": None, "name": None, "balance": None, "available": None}

        try:
            # Account name from the header h2
            name_el = card.locator('[data-cy="general-drawer-header"] h2')
            name_text = await name_el.text_content()
            if name_text:
                acct["name"] = name_text.strip()

            # Last 4 digits — try data-cy="account-last-4" first
            last4_el = card.locator('[data-cy="account-last-4"]')
            if await last4_el.count() > 0:
                last4_text = await last4_el.first.text_content()
                if last4_text:
                    acct["last_four"] = last4_text.strip()
            else:
                # Fallback: extract from card-link-button text "Account ending in 8814"
                link_btn = card.locator('[data-cy="card-link-button"]')
                if await link_btn.count() > 0:
                    link_text = await link_btn.text_content()
                    if link_text:
                        match = re.search(r'(\d{4})\s*$', link_text.strip())
                        if match:
                            acct["last_four"] = match.group(1)

            # Current Balance — data-cy="section-1.value"
            bal_el = card.locator('[data-cy="section-1.value"]')
            if await bal_el.count() > 0:
                bal_text = await bal_el.text_content()
                if bal_text:
                    acct["balance"] = _parse_dollar(bal_text)

            # Available to spend — data-cy="section-2.value"
            avail_el = card.locator('[data-cy="section-2.value"]')
            if await avail_el.count() > 0:
                avail_text = await avail_el.text_content()
                if avail_text:
                    acct["available"] = _parse_dollar(avail_text)

            if acct["last_four"]:
                accounts.append(acct)
                logger.info(f"  Account ···{acct['last_four']}: balance={acct['balance']}, available={acct['available']}")

        except Exception as e:
            logger.warning(f"Error scraping card {i}: {e}")
            continue

    return accounts


async def _scrape_account_transactions(
    page: Page, acct: dict, acct_info: dict
) -> list[dict]:
    """
    Click Activity for a specific account, scrape the transaction table.
    """
    last4 = acct["last_four"]
    account_id = acct_info["account_id"]
    transactions = []

    try:
        # Find and click the Activity button for this specific card
        # The Activity button ID contains the card's UUID, but we can match by
        # finding the card container with this last4, then clicking its Activity button
        cards = page.locator('[data-testid="desktop-card-container"]')
        card_count = await cards.count()

        target_card = None
        for i in range(card_count):
            card = cards.nth(i)
            # Check if this card matches our last4
            last4_el = card.locator('[data-cy="account-last-4"]')
            if await last4_el.count() > 0:
                text = await last4_el.first.text_content()
                if text and text.strip() == last4:
                    target_card = card
                    break
            # Fallback for Amazon card which uses a link button
            link_btn = card.locator('[data-cy="card-link-button"]')
            if await link_btn.count() > 0:
                link_text = await link_btn.text_content()
                if link_text and last4 in link_text:
                    target_card = card
                    break

        if not target_card:
            logger.warning(f"Could not find card for ···{last4} on dashboard")
            return []

        # Click Activity button within this card
        activity_btn = target_card.locator('button[data-reason="activity"]')
        await activity_btn.click()
        await page.wait_for_timeout(3000)

        # Now we should be on the activity page or a slide-in panel
        # Wait for the transaction table to load
        try:
            await page.wait_for_selector("table", timeout=15000)
        except Exception:
            logger.warning(f"No transaction table found for ···{last4}")
            return []

        # Scrape all transaction rows
        rows = page.locator("table tbody tr")
        row_count = await rows.count()
        logger.info(f"  Found {row_count} transaction rows for ···{last4}")

        for j in range(row_count):
            row = rows.nth(j)
            txn = _parse_synchrony_row(row, account_id, last4)
            if txn:
                parsed = await txn
                if parsed:
                    transactions.append(parsed)

    except Exception as e:
        logger.warning(f"Error scraping transactions for ···{last4}: {e}")

    return transactions


async def _parse_synchrony_row(row, account_id: str, last4: str) -> dict | None:
    """Parse a single transaction row from Synchrony's activity table."""
    try:
        # Date — second td contains a span with date text
        date_el = row.locator("td").nth(1).locator("span")
        date_text = await date_el.text_content()
        if not date_text:
            return None
        date_text = date_text.strip()

        # Parse "Mar 20 2026" format
        try:
            parsed_date = datetime.strptime(date_text, "%b %d %Y").strftime("%Y-%m-%d")
        except ValueError:
            try:
                parsed_date = datetime.strptime(date_text, "%B %d %Y").strftime("%Y-%m-%d")
            except ValueError:
                logger.warning(f"Unparseable date: {date_text}")
                return None

        # Description — third td contains a button with aria-label or p.description
        desc_el = row.locator("td").nth(2).locator("button")
        description = ""
        if await desc_el.count() > 0:
            description = (await desc_el.get_attribute("aria-label") or "").strip()
        if not description:
            p_el = row.locator("td").nth(2).locator("p").first
            if await p_el.count() > 0:
                description = (await p_el.text_content() or "").strip()

        if not description:
            return None

        # Amount — fifth td contains a p with the dollar amount
        amount_el = row.locator("td").nth(4).locator("p")
        amount_text = ""
        if await amount_el.count() > 0:
            amount_text = (await amount_el.text_content() or "").strip()

        if not amount_text:
            return None

        amount = _parse_dollar(amount_text)
        if amount is None:
            return None

        # Determine sign: payments are negative (with - prefix), purchases are positive in Synchrony
        # In our system: purchases (expenses) should be negative, payments should be positive
        # Synchrony shows: payments as "-$500.00", purchases as "$1865.85"
        # So we flip: positive Synchrony amount = purchase = negative in our system
        if not amount_text.startswith("-"):
            amount = -abs(amount)  # Purchase → negative (expense)
        else:
            amount = abs(amount)   # Payment → positive (cc_payment)

        # Type hint from first td
        type_el = row.locator("td").first.locator("span[color]")
        type_text = ""
        if await type_el.count() > 0:
            type_text = (await type_el.text_content() or "").strip().lower()

        return {
            "date": parsed_date,
            "description": description,
            "amount": amount,
            "account_id": account_id,
            "last_four": last4,
            "type_hint": type_text,
            "import_source": "synchrony_scrape",
        }

    except Exception as e:
        logger.warning(f"Error parsing Synchrony row: {e}")
        return None


def _parse_dollar(text: str) -> float | None:
    """Parse a dollar string like '$965.85' or '-$500.00' into a float."""
    if not text:
        return None
    cleaned = text.strip().replace("$", "").replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None