#!/usr/bin/env python3
"""
Subscription Renewal Warning Tool
Reads Apple / Google Play export files and pops up a desktop warning
before any subscription charges — especially sneaky weekly ones.
"""

import json
import csv
import sys
import argparse
from datetime import datetime, timedelta
from dataclasses import dataclass
from pathlib import Path


# ─── Data model ───────────────────────────────────────────────────────────────

@dataclass
class Subscription:
    name: str
    renewal_date: datetime
    price: float
    currency: str
    period: str   # "weekly", "monthly", "yearly", …
    source: str   # "apple", "google", "manual"

    @property
    def days_until_renewal(self) -> int:
        return (self.renewal_date.date() - datetime.now().date()).days

    @property
    def is_weekly(self) -> bool:
        return "week" in self.period.lower()


# ─── Notifications ────────────────────────────────────────────────────────────

def send_notification(title: str, message: str) -> None:
    """Desktop popup with two fallbacks so something always shows."""
    sent = False

    # 1. plyer (cross-platform, best)
    try:
        from plyer import notification as plyer_notify
        plyer_notify.notify(title=title, message=message,
                            app_name="Subscription Monitor", timeout=20)
        sent = True
    except Exception:
        pass

    # 2. tkinter popup
    if not sent:
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            messagebox.showwarning(title, message)
            root.destroy()
            sent = True
        except Exception:
            pass

    # 3. console fallback
    if not sent:
        border = "=" * 55
        print(f"\n{border}\n  WARNING: {title}\n  {message}\n{border}\n")


# ─── Parsers ──────────────────────────────────────────────────────────────────

def _parse_date(date_str: str) -> datetime | None:
    """Try several common date formats; return None on failure."""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y",
                "%B %d, %Y", "%b %d, %Y",
                "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except ValueError:
            continue
    return None


def parse_apple_export(filepath: str) -> list[Subscription]:
    """
    Parse the CSV Apple gives you from privacy.apple.com.

    How to get it:
      1. Go to  privacy.apple.com
      2. "Request a copy of your data"
      3. Tick "App Store, iTunes Store, iBooks Store, and Apple Music"
      4. Wait for the email → download → unzip
      5. Look for a file like  'App Store, iTunes Store.csv'
         or  'Apple_Media_Services/App_Store/Subscription_History.csv'
    """
    subs: list[Subscription] = []
    path = Path(filepath)
    if not path.exists():
        print(f"[apple] File not found: {filepath}")
        return subs

    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("Title") or row.get("App Name") or
                    row.get("Subscription Name") or row.get("Product Name") or "Unknown")

            date_raw = (row.get("Renewal Date") or row.get("Next Renewal") or
                        row.get("Expires Date") or row.get("Subscription Renewal Date") or "")

            price_raw = (row.get("Amount") or row.get("Price") or
                         row.get("Subscription Price") or "0")

            period = (row.get("Subscription Duration") or row.get("Period") or
                      row.get("Billing Period") or "unknown")

            if not date_raw:
                continue

            renewal_date = _parse_date(date_raw)
            if renewal_date is None:
                continue

            try:
                price = float(price_raw.replace("$", "").replace(",", "").strip())
            except (ValueError, AttributeError):
                price = 0.0

            subs.append(Subscription(
                name=name.strip(),
                renewal_date=renewal_date,
                price=price,
                currency="USD",
                period=period.strip(),
                source="apple",
            ))

    print(f"[apple] Loaded {len(subs)} subscription(s).")
    return subs


def parse_google_export(filepath: str) -> list[Subscription]:
    """
    Parse the JSON Google Takeout gives you.

    How to get it:
      1. Go to  myaccount.google.com/data-and-privacy
      2. "Download your data"  (Google Takeout)
      3. Deselect all → tick only "Google Play Store"
      4. Download → unzip
      5. Look inside  'Takeout/Google Play Store/Subscriptions/'
         — each JSON file is one subscription, or there may be one big file.
    """
    subs: list[Subscription] = []
    path = Path(filepath)
    if not path.exists():
        print(f"[google] File not found: {filepath}")
        return subs

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    items = data if isinstance(data, list) else data.get("subscriptions", [data])

    for item in items:
        name = item.get("title") or item.get("packageName") or "Unknown App"

        # Expiry can be a ISO string or an epoch-millis integer
        date_raw = (item.get("expiryTime") or item.get("expiryTimeMillis") or
                    item.get("nextBillingDate") or "")

        if not date_raw:
            continue

        try:
            if str(date_raw).isdigit():
                renewal_date = datetime.fromtimestamp(int(str(date_raw)[:10]))
            else:
                renewal_date = _parse_date(str(date_raw))
                if renewal_date is None:
                    continue
        except Exception:
            continue

        micros = item.get("priceAmountMicros", 0)
        price = int(micros) / 1_000_000 if micros else 0.0
        currency = item.get("priceCurrencyCode", "USD")

        # Google uses paymentState or subscriptionPeriod for cadence
        period = (item.get("subscriptionPeriod") or item.get("paymentState") or "unknown")
        # P1W = 1 week, P1M = 1 month  (ISO 8601 duration)
        if period == "P1W":
            period = "weekly"
        elif period == "P1M":
            period = "monthly"
        elif period == "P1Y":
            period = "yearly"

        subs.append(Subscription(
            name=name.strip(),
            renewal_date=renewal_date,
            price=price,
            currency=currency,
            period=period,
            source="google",
        ))

    print(f"[google] Loaded {len(subs)} subscription(s).")
    return subs


def load_manual_subscriptions(filepath: str = "subscriptions.json") -> list[Subscription]:
    """Load hand-typed subscriptions from a JSON file (see --sample)."""
    subs: list[Subscription] = []
    path = Path(filepath)
    if not path.exists():
        return subs

    with open(path) as f:
        data = json.load(f)

    for item in data:
        renewal_date = _parse_date(item.get("renewal_date", ""))
        if renewal_date is None:
            print(f"[manual] Skipping '{item.get('name', '?')}' — bad date.")
            continue
        subs.append(Subscription(
            name=item["name"],
            renewal_date=renewal_date,
            price=float(item.get("price", 0)),
            currency=item.get("currency", "USD"),
            period=item.get("period", "unknown"),
            source="manual",
        ))

    print(f"[manual] Loaded {len(subs)} subscription(s).")
    return subs


def create_sample_file() -> None:
    sample = [
        {
            "name": "Sneaky Weekly App",
            "renewal_date": (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d"),
            "price": 14.99,
            "currency": "USD",
            "period": "weekly",
        },
        {
            "name": "Normal Monthly App",
            "renewal_date": (datetime.now() + timedelta(days=12)).strftime("%Y-%m-%d"),
            "price": 9.99,
            "currency": "USD",
            "period": "monthly",
        },
    ]
    with open("subscriptions.json", "w") as f:
        json.dump(sample, f, indent=2)
    print("Created subscriptions.json — edit it with your real subscriptions.")


# ─── Core check ───────────────────────────────────────────────────────────────

def check_and_warn(subscriptions: list[Subscription], warn_days: int = 3) -> None:
    if not subscriptions:
        print("No subscriptions loaded. See --help for data sources.")
        return

    print(f"\n{'─'*55}")
    print(f"  Checking {len(subscriptions)} subscription(s)  "
          f"(warning window: {warn_days} days)")
    print(f"{'─'*55}\n")

    upcoming: list[Subscription] = []

    for sub in sorted(subscriptions, key=lambda s: s.renewal_date):
        days = sub.days_until_renewal

        if days < 0:
            timing = f"expired {abs(days)}d ago"
        elif days == 0:
            timing = "RENEWS TODAY !!!"
        elif days <= warn_days:
            timing = f"renews in {days} day(s)  ← WARNING"
        else:
            timing = f"renews in {days} days"

        weekly_tag = "  [WEEKLY — charges every 7 days!]" if sub.is_weekly else ""
        alert_marker = ">>>" if days <= warn_days and days >= 0 else "   "

        print(f"{alert_marker} {sub.name}{weekly_tag}")
        print(f"      {timing}")
        print(f"      ${sub.price:.2f} / {sub.period}  |  "
              f"next: {sub.renewal_date.strftime('%Y-%m-%d')}  |  source: {sub.source}")
        print()

        if 0 <= days <= warn_days:
            upcoming.append(sub)

    if not upcoming:
        print(f"✓  No renewals within the next {warn_days} days.")
        return

    print(f"\n{'!'*55}")
    print(f"  {len(upcoming)} renewal(s) coming up — sending notification(s)")
    print(f"{'!'*55}\n")

    for sub in upcoming:
        days = sub.days_until_renewal
        timing = "TODAY" if days == 0 else f"in {days} day(s)"
        weekly_note = ("\n⚠  This is a WEEKLY subscription.\n"
                       "   It charges every 7 days — cancel 24h before to avoid the next charge.")

        send_notification(
            title=f"Subscription renews {timing}: {sub.name}",
            message=(
                f"${sub.price:.2f} / {sub.period} will be charged {timing}.\n"
                f"Renewal date: {sub.renewal_date.strftime('%Y-%m-%d')}"
                f"{weekly_note if sub.is_weekly else ''}"
            ),
        )


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Warn before subscription renewals — especially weekly traps.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
DATA SOURCES
  --apple FILE    CSV from privacy.apple.com  (see instructions in source)
  --google FILE   JSON from Google Takeout    (see instructions in source)
  --manual        subscriptions.json in current directory

QUICK START
  python subscription_monitor.py --sample        # create a starter JSON
  python subscription_monitor.py                 # reads subscriptions.json
  python subscription_monitor.py --days 5        # warn 5 days out (default 3)

AUTOMATE IT (macOS cron — run daily at 9am)
  crontab -e
  0 9 * * * /usr/bin/python3 /path/to/subscription_monitor.py
""",
    )
    parser.add_argument("--apple",  metavar="FILE", help="Apple privacy export CSV")
    parser.add_argument("--google", metavar="FILE", help="Google Takeout subscriptions JSON")
    parser.add_argument("--manual", action="store_true", help="Load subscriptions.json")
    parser.add_argument("--sample", action="store_true",
                        help="Create a sample subscriptions.json and exit")
    parser.add_argument("--days",   type=int, default=3,
                        help="Days before renewal to warn (default: 3)")
    args = parser.parse_args()

    if args.sample:
        create_sample_file()
        return

    all_subs: list[Subscription] = []

    if args.apple:
        all_subs.extend(parse_apple_export(args.apple))

    if args.google:
        all_subs.extend(parse_google_export(args.google))

    # Always load manual file if present; --manual flag makes it required
    manual_path = Path("subscriptions.json")
    if args.manual or manual_path.exists():
        all_subs.extend(load_manual_subscriptions())

    if not all_subs and not args.apple and not args.google:
        print("No data source specified. Run:  python subscription_monitor.py --help")
        print("Quick start:                    python subscription_monitor.py --sample")
        sys.exit(1)

    check_and_warn(all_subs, warn_days=args.days)


if __name__ == "__main__":
    main()
