"""Create a platform admin: python scripts/create_admin.py email password"""
import asyncio
import sys

from app.core.database import SessionLocal
from app.core.security import hash_password
from app.models import PlatformAdmin


async def main(email: str, password: str):
    async with SessionLocal() as db:
        db.add(PlatformAdmin(email=email, password_hash=hash_password(password)))
        await db.commit()
    print(f"Platform admin created: {email}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
