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
CONSUMER_CENTER = "https://consumercenter.mysynchrony.com/consumercenter/login/?client=paysol"
LOGIN_URL = "https://id.synchrony.com/idp/en"

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


async def scrape_synchrony(session, username, password):
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
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
            # ── STEP 1: Navigate to consumer center ──
            session["status"] = "logging_in"
            logger.info("Navigating to Synchrony consumer center")
            await page.goto(CONSUMER_CENTER, wait_until="commit", timeout=60000)
            await page.wait_for_timeout(5000)

            # ── STEP 2: Click YES CONTINUE if migration page appears ──
            try:
                yes_btn = page.locator('button[data-title="yes_continue"]')
                await yes_btn.wait_for(state="visible", timeout=10000)
                await yes_btn.click()
                logger.info("Clicked YES, CONTINUE")
                await page.wait_for_timeout(5000)
            except Exception:
                logger.info("No YES CONTINUE page — checking for login form")

            # ── STEP 3: Fill login form ──
            # We might be on id.synchrony.com already or need to wait for redirect
            try:
                await page.wait_for_url("**/id.synchrony.com/**", timeout=15000)
            except Exception:
                if "id.synchrony.com" not in page.url:
                    logger.warning(f"Not on login page yet, URL: {page.url}")
                    await page.screenshot(path="/tmp/synchrony_pre_login.png")
                    await page.goto(LOGIN_URL, wait_until="commit", timeout=60000)
                    await page.wait_for_timeout(5000)

            logger.info(f"On login page: {page.url}")

            username_field = page.locator("input#username")
            await username_field.wait_for(state="visible", timeout=15000)
            await username_field.fill(username)

            password_field = page.locator("input#password")
            await password_field.fill(password)
            await page.wait_for_timeout(500)

            submit_btn = page.locator('button[type="submit"]')
            await submit_btn.click()
            await page.wait_for_timeout(5000)

            try:
                await page.wait_for_url("**/accounts/**", timeout=30000)
            except Exception:
                content = await page.content()
                if "security" in content.lower() or "verify" in content.lower():
                    raise Exception("Synchrony requested security verification")
                if "incorrect" in content.lower() or "invalid" in content.lower():
                    raise Exception("Login failed — incorrect credentials")
                logger.warning(f"Unexpected URL after login: {page.url}")

            session["status"] = "authenticated"
            logger.info(f"Authenticated — on {page.url}")
            await page.wait_for_timeout(3000)

            # ── STEP 4: Scrape balances ──
            session["status"] = "scraping_balance"
            accounts_data = await _scrape_dashboard_balances(page)
            session["accounts"] = accounts_data
            logger.info(f"Scraped balances for {len(accounts_data)} accounts")

            # ── STEP 5: Scrape transactions ──
            session["status"] = "scraping_transactions"
            all_transactions = []

            for acct in accounts_data:
                last4 = acct.get("last_four", "")
                acct_info = ACCOUNT_MAP.get(last4)
                if not acct_info:
                    logger.warning(f"Unknown account ···{last4} — skipping")
                    continue

                logger.info(f"Scraping transactions for {acct_info['name']} (···{last4})")
                txns = await _scrape_account_transactions(page, acct, acct_info)
                all_transactions.extend(txns)
                logger.info(f"  Found {len(txns)} transactions")

                await page.goto(SYNCHRONY_URL, wait_until="commit", timeout=30000)
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


async def _scrape_dashboard_balances(page):
    accounts = []
    cards = page.locator('[data-testid="desktop-card-container"]')
    card_count = await cards.count()
    logger.info(f"Found {card_count} account cards on dashboard")

    for i in range(card_count):
        card = cards.nth(i)
        acct = {"last_four": None, "name": None, "balance": None, "available": None}

        try:
            name_el = card.locator('[data-cy="general-drawer-header"] h2')
            name_text = await name_el.text_content()
            if name_text:
                acct["name"] = name_text.strip()

            last4_el = card.locator('[data-cy="account-last-4"]')
            if await last4_el.count() > 0:
                last4_text = await last4_el.first.text_content()
                if last4_text:
                    acct["last_four"] = last4_text.strip()
            else:
                link_btn = card.locator('[data-cy="card-link-button"]')
                if await link_btn.count() > 0:
                    link_text = await link_btn.text_content()
                    if link_text:
                        match = re.search(r'(\d{4})\s*$', link_text.strip())
                        if match:
                            acct["last_four"] = match.group(1)

            bal_el = card.locator('[data-cy="section-1.value"]')
            if await bal_el.count() > 0:
                bal_text = await bal_el.text_content()
                if bal_text:
                    acct["balance"] = _parse_dollar(bal_text)

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


async def _scrape_account_transactions(page, acct, acct_info):
    last4 = acct["last_four"]
    account_id = acct_info["account_id"]
    transactions = []

    try:
        cards = page.locator('[data-testid="desktop-card-container"]')
        card_count = await cards.count()

        target_card = None
        for i in range(card_count):
            card = cards.nth(i)
            last4_el = card.locator('[data-cy="account-last-4"]')
            if await last4_el.count() > 0:
                text = await last4_el.first.text_content()
                if text and text.strip() == last4:
                    target_card = card
                    break
            link_btn = card.locator('[data-cy="card-link-button"]')
            if await link_btn.count() > 0:
                link_text = await link_btn.text_content()
                if link_text and last4 in link_text:
                    target_card = card
                    break

        if not target_card:
            logger.warning(f"Could not find card for ···{last4} on dashboard")
            return []

        activity_btn = target_card.locator('button[data-reason="activity"]')
        await activity_btn.click()
        await page.wait_for_timeout(3000)

        try:
            await page.wait_for_selector("table", timeout=15000)
        except Exception:
            logger.warning(f"No transaction table found for ···{last4}")
            return []

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


async def _parse_synchrony_row(row, account_id, last4):
    try:
        date_el = row.locator("td").nth(1).locator("span")
        date_text = await date_el.text_content()
        if not date_text:
            return None
        date_text = date_text.strip()

        try:
            parsed_date = datetime.strptime(date_text, "%b %d %Y").strftime("%Y-%m-%d")
        except ValueError:
            try:
                parsed_date = datetime.strptime(date_text, "%B %d %Y").strftime("%Y-%m-%d")
            except ValueError:
                logger.warning(f"Unparseable date: {date_text}")
                return None

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

        amount_el = row.locator("td").nth(4).locator("p")
        amount_text = ""
        if await amount_el.count() > 0:
            amount_text = (await amount_el.text_content() or "").strip()

        if not amount_text:
            return None

        amount = _parse_dollar(amount_text)
        if amount is None:
            return None

        if not amount_text.startswith("-"):
            amount = -abs(amount)
        else:
            amount = abs(amount)

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


def _parse_dollar(text):
    if not text:
        return None
    cleaned = text.strip().replace("$", "").replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None
