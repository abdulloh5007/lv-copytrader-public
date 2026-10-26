"""Manage launch signups from the operator's container, without a public email API.

python -m app.cli.waitlist count
python -m app.cli.waitlist export > waitlist.csv
python -m app.cli.waitlist delete --email person@example.com
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import sys

from sqlalchemy import delete, func, select

from app.api.waitlist import Signup, digest
from app.crypto import decrypt
from app.db import dispose_db, session_scope
from app.models import WaitlistEntry


async def manage(command: str, email: str | None = None) -> int:
    try:
        async with session_scope() as session:
            if command == "count":
                print(await session.scalar(select(func.count()).select_from(WaitlistEntry)))
            elif command == "delete":
                address = Signup(email=email, consent=True).email
                result = await session.execute(delete(WaitlistEntry).where(WaitlistEntry.email_hash == digest("email:" + address)))
                print(f"Deleted: {result.rowcount}")
            elif command == "export":
                writer = csv.writer(sys.stdout)
                writer.writerow(["email", "consented_at_utc", "consent_version"])
                rows = await session.stream_scalars(select(WaitlistEntry).order_by(WaitlistEntry.id))
                async for entry in rows:
                    address = decrypt(entry.email_encrypted)
                    # Prevent formula execution when the CSV is opened in a spreadsheet.
                    if address.startswith(("=", "+", "-", "@", "\t", "\r")):
                        address = "'" + address
                    writer.writerow([address, entry.created_at.isoformat(), entry.consent_version])
        return 0
    finally:
        await dispose_db()


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage encrypted waitlist signups")
    parser.add_argument("command", choices=["count", "export", "delete"])
    parser.add_argument("--email")
    args = parser.parse_args()
    if args.command == "delete" and not args.email:
        parser.error("delete requires --email")
    raise SystemExit(asyncio.run(manage(args.command, args.email)))


if __name__ == "__main__":
    main()
