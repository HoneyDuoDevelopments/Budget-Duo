"""
Apple Card Scraper — Playwright automation
Handles: login → 2FA → balance scrape → CSV export with date range

Flow:
1. Navigate to card.apple.com
2. Click Sign In → Apple ID modal (iframe)
3. Enter phone number → Continue → Continue with Password → Enter password → Sign In
4. 2FA: Apple sends SMS, we pause and wait for code from Budget Duo UI
5. Enter 6-digit code into individual input fields (auto-submits)
6. Scrape balance from .card-balance-balance and .card-balance-credit
7. Navigate to Statements → Export Transactions
8. Set date range via date picker → Export CSV
9. Parse CSV → return transaction data
"""
import asyncio
import csv
import io
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

from playwright.async_api import async_playwright, Page, BrowserContext

logger = logging.getLogger(__name__)

APPLE_CARD_URL = "https://card.apple.com"
DOWNLOAD_DIR = "/tmp/apple_card_exports"
TWO_FA_TIMEOUT = 180  # 3 minutes to enter 2FA code
POLL_INTERVAL = 1     # Check for 2FA code every second


async def scrape_apple_card(
    session: dict,
    apple_id: str,
    password: str,
    start_date: str,
    end_date: str,
    backfill: bool,
) -> dict:
    """
    Full Apple Card scrape: login, 2FA, balance, CSV export.
    Updates session dict in-place for status polling.
    Returns final result dict to merge into session.
    """
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        context = await browser.new_context(
            accept_downloads=True,
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        try:
            # ── STEP 1: Navigate & click Sign In ──
            session["status"] = "logging_in"
            logger.info("Navigating to card.apple.com")
            await page.goto(APPLE_CARD_URL, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2000)

            # Click the Sign In button on the landing page
            sign_in_btn = page.locator("ui-button.sign-in-button")
            await sign_in_btn.click()
            await page.wait_for_timeout(3000)

            # ── STEP 2: Apple ID auth (iframe) ──
            # Apple's auth is in an iframe — find it
            auth_frame = None
            for frame in page.frames:
                if "idmsa.apple.com" in (frame.url or ""):
                    auth_frame = frame
                    break

            if not auth_frame:
                # Sometimes it's not an iframe but a redirect
                # Check if we're on the Apple ID page directly
                if "idmsa.apple.com" in page.url or "appleid.apple.com" in page.url:
                    auth_frame = page
                else:
                    # Wait a bit more and try again
                    await page.wait_for_timeout(3000)
                    for frame in page.frames:
                        if "idmsa.apple.com" in (frame.url or "") or "appleid.apple.com" in (frame.url or ""):
                            auth_frame = frame
                            break

            if not auth_frame:
                raise Exception("Could not find Apple ID login frame")

            logger.info("Found Apple auth frame, entering credentials")

            # Enter Apple ID (phone number)
            username_field = auth_frame.locator("#account_name_text_field")
            await username_field.wait_for(state="visible", timeout=15000)
            await username_field.fill(apple_id)
            await page.wait_for_timeout(500)

            # Click Continue/Next
            continue_btn = auth_frame.locator("#sign-in")
            await continue_btn.click()
            await page.wait_for_timeout(3000)

            # Click "Continue with Password" (may not appear if Apple goes straight to password)
            try:
                pwd_btn = auth_frame.locator("#continue-password")
                await pwd_btn.wait_for(state="visible", timeout=5000)
                await pwd_btn.click()
                await page.wait_for_timeout(2000)
            except Exception:
                logger.info("No 'Continue with Password' button — may already be on password step")

            # Enter password
            password_field = auth_frame.locator("#password_text_field")
            await password_field.wait_for(state="visible", timeout=10000)
            await password_field.fill(password)
            await page.wait_for_timeout(500)

            # Click Sign In
            signin_btn = auth_frame.locator("#sign-in")
            await signin_btn.click()
            await page.wait_for_timeout(3000)

            # ── STEP 3: 2FA ──
            session["status"] = "awaiting_2fa"
            logger.info("Waiting for 2FA code from user...")

            # Wait for the 2FA input fields to appear
            try:
                first_digit = auth_frame.locator("input.form-security-code-input").first
                await first_digit.wait_for(state="visible", timeout=15000)
            except Exception:
                # Check if we somehow got through without 2FA
                if "card.apple.com" in page.url and "Payments" in await page.content():
                    logger.info("Authenticated without 2FA!")
                    session["status"] = "authenticated"
                else:
                    raise Exception("2FA input fields not found and not authenticated")

            if session["status"] == "awaiting_2fa":
                # Poll for the 2FA code from the session (set by /api/scrape/apple/verify)
                code = await _wait_for_2fa_code(session)
                if not code:
                    raise Exception("2FA timeout — no code received within 3 minutes")

                logger.info("Entering 2FA code")
                session["status"] = "verifying_2fa"

                # Enter each digit into its own input field
                digits = auth_frame.locator("input.form-security-code-input")
                for i, char in enumerate(code[:6]):
                    digit_input = digits.nth(i)
                    await digit_input.fill(char)
                    await page.wait_for_timeout(200)

                # Apple auto-submits after all 6 digits — wait for redirect
                await page.wait_for_timeout(5000)

                # Check if we landed on the dashboard
                try:
                    await page.wait_for_url("**/card.apple.com/**", timeout=20000)
                except Exception:
                    pass  # URL might not change cleanly

            # ── STEP 4: Verify we're authenticated ──
            session["status"] = "authenticated"
            logger.info("Authenticated — scraping balance")
            await page.wait_for_timeout(8000)
            await page.screenshot(path=f"{DOWNLOAD_DIR}/post_auth.png")
            logger.info(f"Post-auth URL: {page.url}")

            # ── STEP 5: Scrape balance ──
            session["status"] = "scraping_balance"
            balance_data = await _scrape_balance(page)
            session["balance"] = balance_data.get("balance")
            session["available"] = balance_data.get("available")
            logger.info(f"Balance: {balance_data}")

            # ── STEP 6: Navigate to Statements & export CSV ──
            session["status"] = "scraping_transactions"
            logger.info(f"Exporting transactions: {start_date} to {end_date}")

            # Click Statements in the left sidebar nav
            # Use exact selector from Apple's nav HTML
            statements_link = page.locator('a.menu-item-link[href="/statements"]')
            try:
                await statements_link.wait_for(state="visible", timeout=15000)
                await statements_link.click()
                logger.info("Clicked Statements via href selector")
            except Exception:
                # Fallback: click by label text inside nav
                try:
                    statements_link = page.locator('.menu-item-label:has-text("Statements")')
                    await statements_link.click()
                    logger.info("Clicked Statements via label text")
                except Exception:
                    await page.screenshot(path=f"{DOWNLOAD_DIR}/pre_statements.png")
                    raise Exception("Could not find Statements nav link")

            await page.wait_for_timeout(5000)

            # Click "Export Transactions" — try multiple selectors
            export_clicked = False
            for selector in [
                'ui-button.export-transactions-button',
                'button:has-text("Export Transactions")',
                'text="Export Transactions"',
                '[class*="export-transactions"]',
            ]:
                try:
                    el = page.locator(selector).first
                    await el.wait_for(state="visible", timeout=5000)
                    await el.click()
                    export_clicked = True
                    logger.info(f"Export Transactions clicked via: {selector}")
                    break
                except Exception:
                    continue

            if not export_clicked:
                # Last resort — screenshot and log what's on the page
                await page.screenshot(path=f"{DOWNLOAD_DIR}/statements_page.png")
                page_text = await page.inner_text("body")
                logger.error(f"Could not find Export Transactions. Page text preview: {page_text[:500]}")
                raise Exception("Could not find Export Transactions button on Statements page")

            await page.wait_for_timeout(2000)

            # Set date range
            await _set_export_date_range(page, start_date, end_date)

            # Click Export and download CSV
            csv_data = await _download_csv(page, context)

            if not csv_data:
                raise Exception("CSV download failed — no data received")

            # ── STEP 7: Parse CSV ──
            session["status"] = "importing"
            transactions = _parse_apple_csv(csv_data)
            session["transactions"] = transactions
            session["txn_count"] = len(transactions)

            logger.info(f"Parsed {len(transactions)} transactions from Apple Card CSV")

            session["status"] = "complete"
            return {
                "status": "complete",
                "balance": balance_data.get("balance"),
                "available": balance_data.get("available"),
                "transactions": transactions,
                "txn_count": len(transactions),
            }

        except Exception as e:
            logger.exception(f"Apple Card scraper error: {e}")
            # Take screenshot for debugging
            try:
                await page.screenshot(path=f"{DOWNLOAD_DIR}/error_screenshot.png")
                logger.info("Error screenshot saved")
            except Exception:
                pass
            session["status"] = "error"
            session["error"] = str(e)
            return {"status": "error", "error": str(e)}

        finally:
            await browser.close()


async def _wait_for_2fa_code(session: dict) -> str | None:
    """Poll the session dict for a 2FA code, with timeout."""
    elapsed = 0
    while elapsed < TWO_FA_TIMEOUT:
        code = session.get("2fa_code")
        if code:
            return code
        await asyncio.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL
    return None


async def _scrape_balance(page: Page) -> dict:
    """Extract balance and available credit from the Apple Card dashboard."""
    result = {"balance": None, "available": None}

    try:
        # Wait for the balance element
        bal_el = page.locator(".card-balance-balance")
        await bal_el.wait_for(state="visible", timeout=10000)
        bal_text = await bal_el.text_content()
        if bal_text:
            result["balance"] = bal_text.strip().replace("$", "").replace(",", "")
    except Exception as e:
        logger.warning(f"Could not scrape balance: {e}")

    try:
        avail_el = page.locator(".card-balance-credit")
        avail_text = await avail_el.text_content()
        if avail_text:
            # "6,114.53 Available" → extract number
            avail_clean = avail_text.strip().replace("$", "").replace(",", "").split()[0]
            result["available"] = avail_clean
    except Exception as e:
        logger.warning(f"Could not scrape available credit: {e}")

    return result


async def _set_export_date_range(page: Page, start_date: str, end_date: str):
    """
    Set the start and end dates in Apple Card's export dialog.
    Uses the custom date picker with aria-label based day selection.
    """
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    # ── Set START date ──
    # Click the start date button (first .push.secondary after "Start Date" label)
    start_btn = page.locator("text=Start Date").locator("..").locator("ui-button.push.secondary")
    try:
        await start_btn.click()
        await page.wait_for_timeout(1000)
    except Exception:
        # Fallback: click the first date button in the export dialog
        date_buttons = page.locator("ui-button.push.secondary")
        await date_buttons.first.click()
        await page.wait_for_timeout(1000)

    await _navigate_to_date(page, start_dt)

    # ── Set END date ──
    end_btn = page.locator("text=End Date").locator("..").locator("ui-button.push.secondary")
    try:
        await end_btn.click()
        await page.wait_for_timeout(1000)
    except Exception:
        date_buttons = page.locator("ui-button.push.secondary")
        await date_buttons.nth(1).click()
        await page.wait_for_timeout(1000)

    await _navigate_to_date(page, end_dt)


async def _navigate_to_date(page: Page, target: datetime):
    """
    Navigate the Apple Card date picker to a specific date.
    Uses month navigation arrows and aria-label day buttons.
    """
    # Format the target date's aria-label: "March 1, 2026"
    target_label = target.strftime("%B %-d, %Y")
    target_month_year = target.strftime("%B %Y")

    # Check current month display
    max_nav = 24  # Don't go back more than 24 months
    for _ in range(max_nav):
        # Read current month/year from the picker header
        month_year_text = await page.locator(".month-year-text").text_content()
        if not month_year_text:
            break

        current = month_year_text.strip()
        if current == target_month_year:
            break

        # Need to navigate — determine direction
        current_dt = datetime.strptime(current, "%B %Y")
        target_month_dt = datetime(target.year, target.month, 1)

        if target_month_dt < current_dt:
            # Go back
            prev_btn = page.locator("ui-button.month-nav-button").first
            await prev_btn.click()
        else:
            # Go forward
            next_btn = page.locator("ui-button.month-nav-button").last
            await next_btn.click()
        await page.wait_for_timeout(500)

    # Click the target day by aria-label
    day_btn = page.locator(f'ui-button[aria-label="{target_label}"]')
    try:
        await day_btn.click()
        await page.wait_for_timeout(500)
    except Exception as e:
        logger.warning(f"Could not click date {target_label}: {e}")
        raise


async def _download_csv(page: Page, context: BrowserContext) -> str | None:
    """Click Export button and capture the downloaded CSV content."""
    # Ensure CSV format is selected (should be default)
    try:
        format_btn = page.locator("text=Comma Separated Values")
        await format_btn.wait_for(state="visible", timeout=3000)
        # Already selected — no action needed
    except Exception:
        pass

    # Click the Export button and wait for download
    async with page.expect_download(timeout=30000) as download_info:
        export_btn = page.locator("ui-button.push.primary:has-text('Export')")
        await export_btn.click()

    download = await download_info.value
    # Save to temp path and read content
    save_path = os.path.join(DOWNLOAD_DIR, download.suggested_filename or "export.csv")
    await download.save_as(save_path)

    with open(save_path, "r") as f:
        content = f.read()

    logger.info(f"Downloaded CSV: {len(content)} bytes, saved to {save_path}")
    return content


def _parse_apple_csv(csv_content: str) -> list[dict]:
    """
    Parse Apple Card CSV export into transaction dicts.
    Expected columns: Transaction Date, Clearing Date, Description,
    Merchant, Category, Type, Amount (in various formats Apple may use)
    """
    transactions = []
    reader = csv.DictReader(io.StringIO(csv_content))

    for row in reader:
        try:
            # Apple Card CSV column names (may vary slightly)
            txn_date = (
                row.get("Transaction Date")
                or row.get("Date")
                or row.get("transaction_date")
                or ""
            ).strip()

            description = (
                row.get("Description")
                or row.get("Merchant")
                or row.get("description")
                or ""
            ).strip()

            merchant = (
                row.get("Merchant")
                or row.get("merchant")
                or ""
            ).strip()

            amount_str = (
                row.get("Amount (USD)")
                or row.get("Amount")
                or row.get("amount")
                or "0"
            ).strip()

            category = (
                row.get("Category")
                or row.get("category")
                or ""
            ).strip()

            txn_type = (
                row.get("Type")
                or row.get("type")
                or ""
            ).strip()

            if not txn_date or not description:
                continue

            # Parse date — Apple uses MM/DD/YYYY or YYYY-MM-DD
            if "/" in txn_date:
                parsed_date = datetime.strptime(txn_date, "%m/%d/%Y").strftime("%Y-%m-%d")
            else:
                parsed_date = txn_date  # Assume ISO format

            # Parse amount — Apple Card: purchases are positive, payments are negative
            # We need to flip: expenses should be negative in our system
            amount = float(amount_str.replace("$", "").replace(",", ""))
            if txn_type.lower() in ("purchase", "transaction", ""):
                amount = -abs(amount)  # Expenses are negative
            elif txn_type.lower() in ("payment", "credit", "refund"):
                amount = abs(amount)   # Payments/credits are positive

            transactions.append({
                "date": parsed_date,
                "description": description,
                "merchant_clean": merchant if merchant and merchant != description else None,
                "amount": amount,
                "category_hint": category,
                "type_hint": txn_type,
                "import_source": "apple_csv",
            })
        except Exception as e:
            logger.warning(f"Skipping unparseable CSV row: {e} — row: {row}")
            continue

    return transactions