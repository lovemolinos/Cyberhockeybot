import os, re, asyncio, logging, math
from aiohttp import web, ClientSession, FormData
import asyncpg
from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import Message

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
log = logging.getLogger("cyberbot")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
OCR_API_KEY = os.environ.get("OCR_API_KEY", "")

if not BOT_TOKEN or not DATABASE_URL:
    raise SystemExit("Set BOT_TOKEN and DATABASE_URL")


# ---------- POISSON (pure Python, без scipy) ----------
def _poisson_pmf(k, lam):
    if k < 0:
        return 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def _poisson_cdf(k, lam):
    k = int(k)
    if k < 0:
        return 0.0
    return sum(_poisson_pmf(i, lam) for i in range(k + 1))


router = Router()
pool = None


# ---------- DB ----------
async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with pool.acquire() as c:
        await c.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            id SERIAL PRIMARY KEY,
            league TEXT NOT NULL,
            team1 TEXT NOT NULL,
            team2 TEXT NOT NULL,
            s1 INT NOT NULL, s2 INT NOT NULL,
            p1_1 INT, p1_2 INT, p2_1 INT, p2_2 INT, p3_1 INT, p3_2 INT,
            total INT NOT NULL,
            p1_total INT, p2_total INT, p3_total INT,
            match_date DATE DEFAULT CURRENT_DATE,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_league ON matches(league);
        CREATE INDEX IF NOT EXISTS idx_t1 ON matches(team1);
        CREATE INDEX IF NOT EXISTS idx_t2 ON matches(team2);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        """)


async def insert_match(league, team1, team2, s1, s2, periods):
    p1 = periods[0] if periods and len(periods) > 0 else (None, None)
    p2 = periods[1] if periods and len(periods) > 1 else (None, None)
    p3 = periods[2] if periods and len(periods) > 2 else 
