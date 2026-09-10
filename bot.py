import os
import re
import math
import io
import asyncio
import logging
from datetime import date, datetime
from aiohttp import web, ClientSession, FormData
import asyncpg
from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import Message
from PIL import Image

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s'
)
log = logging.getLogger("cyberbot")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
OCR_API_KEY = os.environ.get("OCR_API_KEY", "")

router = Router()
pool = None


# ---------- DATABASE ----------
async def create_pool_with_retry(max_attempts=100, delay=10):
    global pool
    if not DATABASE_URL:
        return None
    for attempt in range(1, max_attempts + 1):
        try:
            pool = await asyncpg.create_pool(
                DATABASE_URL,
                min_size=1,
                max_size=5,
                timeout=60,
                command_timeout=60,
                statement_cache_size=0
            )
            async with pool.acquire() as c:
                await c.execute("SELECT 1")
            log.info(f"=== DB CONNECTED (attempt {attempt}) ===")
            return pool
        except Exception as e:
            log.error(f"=== DB INIT FAILED (try {attempt}): {e} ===")
            pool = None
            await asyncio.sleep(delay)
    return None


async def init_db():
    global pool
    if pool is None:
        await create_pool_with_retry(max_attempts=5, delay=5)
    if pool is None:
        return False
    try:
        async with pool.acquire() as c:
            await c.execute("""
                CREATE TABLE IF NOT EXISTS matches (
                    id SERIAL PRIMARY KEY,
                    match_date DATE,
                    league TEXT NOT NULL,
                    team1 TEXT NOT NULL,
                    team2 TEXT NOT NULL,
                    s1 INT NOT NULL,
                    s2 INT NOT NULL,
                    p1_1 INT, p1_2 INT,
                    p2_1 INT, p2_2 INT,
                    p3_1 INT, p3_2 INT,
                    total INT NOT NULL,
                    p1_total INT, p2_total INT, p3_total INT,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
                ALTER TABLE matches ADD COLUMN IF NOT EXISTS match_date DATE;
                CREATE INDEX IF NOT EXISTS idx_league ON matches(league);
                CREATE INDEX IF NOT EXISTS idx_match_date ON matches(match_date);
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
            """)
        log.info("DB schema ready")
        return True
    except Exception as e:
        log.error(f"DB schema error: {e}")
        return False


async def ensure_pool():
    global pool
    if pool is not None:
        try:
            async with pool.acquire() as c:
                await c.execute("SELECT 1")
            return True
        except Exception:
            try:
                await pool.close()
            except Exception:
                pass
            pool = None
    await create_pool_with_retry(max_attempts=3, delay=3)
    return pool is not None


async def insert_match(league, team1, team2, s1, s2, periods, match_date=None):
    if not await ensure_pool():
        raise RuntimeError("DB_UNAVAILABLE")
    p1 = periods[0] if periods and len(periods) > 0 else (None, None)
    p2 = periods[1] if periods and len(periods) > 1 else (None, None)
    p3 = periods[2] if periods and len(periods) > 2 else (None, None)
    total = s1 + s2
    p1t = (p1[0] + p1[1]) if p1[0] is not None else None
    p2t = (p2[0] + p2[1]) if p2[0] is not None else None
    p3t = (p3[0] + p3[1]) if p3[0] is not None else None

    if match_date is None:
        match_date = date.today()

    async with pool.acquire() as c:
        await c.execute("""
            INSERT INTO matches (
                match_date, league, team1, team2, s1, s2,
                p1_1, p1_2, p2_1, p2_2, p3_1, p3_2,
                total, p1_total, p2_total, p3_total
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
        """, match_date, league, team1, team2, s1, s2,
            p1[0], p1[1], p2[0], p2[1], p3[0], p3[1],
            total, p1t, p2t, p3t)


# ---------- PARSING ----------
SCORE_RE = re.compile(
    r'^(.+?)\s*[-–—]\s*(.+?)\s+(\d+)\s*[:\-]\s*(\d+)'
    r'(?:\s*[\(\[]\s*([^\)\]]+?)\s*[\)\]])?\s*$'
)
LEAGUE_RE = re.compile(r'^\s*[\[\(\{]\s*(.+?)\s*[\]\)\}]\s*$')
DATE_RE = re.compile(r'^\s*[\[\(]?\s*(\d{4}[-.]\d{2}[-.]\d{2})\s*[\]\)]?\s*$')


def parse_block(text, default_league="Общая", default_date=None):
    matches = []
    league = default_league
    current_date = default_date

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        both = re.match(
            r'^\s*\[\s*(\d{4}[-.]\d{2}[-.]\d{2})\s*\]\s*'
            r'[\[\(]\s*(.+?)\s*[\]\)]\s*$',
            line
        )
        if both:
            try:
                ds = both.group(1).replace(".", "-")
                current_date = datetime.strptime(ds, "%Y-%m-%d").date()
            except Exception:
                pass
            league = both.group(2).strip()
            continue

        dm = DATE_RE.match(line)
        if dm:
            try:
                ds = dm.group(1).replace(".", "-")
                current_date = datetime.strptime(ds, "%Y-%m-%d").date()
            except Exception:
                pass
            continue

        lb = LEAGUE_RE.match(line)
        if lb:
            league = lb.group(1).strip()
            continue

        prefix = re.match(r'^([^:\d]{2,40})\s*:\s*(.+)$', line)
        if prefix and SCORE_RE.match(prefix.group(2).strip()):
            league = prefix.group(1).strip()
            line = prefix.group(2).strip()

        m = SCORE_RE.match(line)
        if not m:
            continue

        t1 = m.group(1).strip()
        t2 = m.group(2).strip()
        s1, s2 = int(m.group(3)), int(m.group(4))
        periods = None
        if m.group(5):
            pairs = re.findall(r'(\d+)\s*[:]\s*(\d+)', m.group(5))
            if len(pairs) >= 3:
                periods = [(int(a), int(b)) for a, b in pairs[:3]]

        matches.append({
            "league": league,
            "team1": t1,
            "team2": t2,
            "s1": s1,
            "s2": s2,
            "periods": periods,
            "match_date": current_date,
        })
    return matches


def prepare_image(image_bytes, max_size=1600):
    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode != "RGB":
            img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > max_size:
            ratio = max_size / max(w, h)
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85, optimize=True)
        return buf.getvalue()
    except Exception:
        return image_bytes


async def ocr_image(image_bytes):
    if not OCR_API_KEY:
        return None
    try:
        async with ClientSession() as s:
            form = FormData()
            form.add_field("file", image_bytes, filename="img.jpg",
                           content_type="image/jpeg")
            form.add_field("language", "rus")
            form.add_field("isOverlayRequired", "false")
            form.add_field("OCREngine", "2")
            form.add_field("detectOrientation", "true")
            async with s.post(
                "https://api.ocr.space/parse/image",
                data=form,
                headers={"apikey": OCR_API_KEY},
                timeout=60
            ) as r:
                j = await r.json()
        if j.get("IsErroredOnProcessing"):
            return None
        results = j.get("ParsedResults")
        if not results:
            return None
        return results[0].get("ParsedText", "")
    except Exception as e:
        log.error(f"OCR: {e}")
        return None


# ---------- NICK EXTRACTION ----------
NICK_RE = re.compile(r'\(([^)]+)\)')


def extract_nick(team_name):
    m = NICK_RE.search(team_name)
    return m.group(1).strip() if m else None


# ---------- PLAYER STATS ----------
async def get_player_stats_from_db():
    """Статистика по никам игроков с победами/поражениями."""
    if not await ensure_pool():
        return []
    async with pool.acquire() as c:
        rows = await c.fetch("""
            SELECT team1, team2, s1, s2, total, p1_total, p2_total, p3_total
            FROM matches
            WHERE p1_total IS NOT NULL
        """)

    players = {}

    def add(team, my, opp, total, p1, p2, p3):
        nick = extract_nick(team)
        if not nick:
            return
        if nick not in players:
            players[nick] = {
                "nick": nick, "matches": 0,
                "wins": 0, "losses": 0, "draws": 0,
                "goals_for": 0, "goals_against": 0,
                "total_sum": 0,
                "p1_sum": 0, "p1_n": 0,
                "p2_sum": 0, "p2_n": 0,
                "p3_sum": 0, "p3_n": 0,
            }
        p = players[nick]
        p["matches"] += 1
        if my > opp:
            p["wins"] += 1
        elif my < opp:
            p["losses"] += 1
        else:
            p["draws"] += 1
        p["goals_for"] += my
        p["goals_against"] += opp
        p["total_sum"] += total
        if p1 is not None:
            p["p1_sum"] += p1
            p["p1_n"] += 1
        if p2 is not None:
            p["p2_sum"] += p2
            p["p2_n"] += 1
        if p3 is not None:
            p["p3_sum"] += p3
            p["p3_n"] += 1

    for r in rows:
        add(r["team1"], r["s1"], r["s2"], r["total"],
            r["p1_total"], r["p2_total"], r["p3_total"])
        add(r["team2"], r["s2"], r["s1"], r["total"],
            r["p1_total"], r["p2_total"], r["p3_total"])

    result = []
    for nick, p in players.items():
        if p["matches"] < 2:
            continue
        result.append({
            "nick": nick,
            "matches": p["matches"],
            "wins": p["wins"],
            "losses": p["losses"],
            "draws": p["draws"],
            "win_pct": p["wins"] / p["matches"] * 100,
            "avg_for": p["goals_for"] / p["matches"],
            "avg_against": p["goals_against"] / p["matches"],
            "avg_total": p["total_sum"] / p["matches"],
            "avg_p1": p["p1_sum"] / p["p1_n"] if p["p1_n"] else 0,
            "avg_p2": p["p2_sum"] / p["p2_n"] if p["p2_n"] else 0,
            "avg_p3": p["p3_sum"] / p["p3_n"] if p["p3_n"] else 0,
            "total_goals": p["goals_for"],
        })
    return result


async def get_player_by_nick(nick):
    players = await get_player_stats_from_db()
    for p in players:
        if p["nick"].lower() == nick.lower():
            return p
    for p in players:
        if nick.lower() in p["nick"].lower():
            return p
    return None


# ---------- HANDLERS ----------
@router.message(Command("start"))
async def cmd_start(m: Message):
    await m.answer(
        "🏒 *Cyber Hockey Bot*\n\n"
        "📊 *Топ игроков (ников):*\n"
        "`/top_players` — по забитым\n"
        "`/top_players wins` — по % побед\n"
        "`/top_players win_count` — по числу побед\n"
        "`/top_players total` — по общему тоталу\n"
        "`/top_players against` — по пропущенным\n"
        "`/player [ник]` — детали по игроку\n\n"
        "*Управление:* /db",
        parse_mode="Markdown"
    )


@router.message(Command("help"))
async def cmd_help(m: Message):
    await cmd_start(m)


@router.message(Command("ping"))
async def cmd_ping(m: Message):
    await m.answer("🏓 Pong! Бот живой.")


@router.message(Command("db"))
async def cmd_db(m: Message):
    if pool is None:
        await ensure_pool()
    if pool is None:
        await m.answer("❌ БД недоступна")
        return
    async with pool.acquire() as c:
        r = await c.fetchrow("SELECT COUNT(*) AS n FROM matches")
    await m.answer(f"✅ БД работает. Матчей: {r['n']}")


@router.message(Command("top_players"))
async def cmd_top_players(m: Message):
    if not await ensure_pool():
        await m.answer("❌ БД недоступна")
        return

    await m.answer("⏳ Считаю...")
    players = await get_player_stats_from_db()
    if not players:
        await m.answer("Нет данных.")
        return

    parts = m.text.split(maxsplit=1)
    mode = parts[1].strip().lower() if len(parts) > 1 else "goals"

    if mode == "wins":
        players.sort(key=lambda p: (p["win_pct"], p["wins"]), reverse=True)
        title = "🏆 *Топ игроков по % побед*"
    elif mode == "win_count":
        players.sort(key=lambda p: p["wins"], reverse=True)
        title = "🏆 *Топ игроков по числу побед*"
    elif mode == "total":
        players.sort(key=lambda p: p["avg_total"], reverse=True)
        title = "🏆 *Топ по общему тоталу матчей*"
    elif mode == "against":
        players.sort(key=lambda p: p["avg_against"], reverse=True)
        title = "🏆 *Топ по пропущенным*"
    else:
        players.sort(key=lambda p: p["avg_for"], reverse=True)
        title = "🏆 *Топ игроков по забитым*"

    lines = [title + "\n"]
    for i, p in enumerate(players[:15], 1):
        lines.append(
            f"{i}. *{p['nick']}* — {p['matches']} матчей\n"
            f"   🏅 {p['wins']}В / {p['losses']}П / {p['draws']}Н "
            f"({p['win_pct']:.0f}% побед)\n"
            f"   ⚽ Забивает {p['avg_for']:.2f} | Пропускает {p['avg_against']:.2f}\n"
            f"   📊 Общий тотал: {p['avg_total']:.2f} "
            f"(P1 {p['avg_p1']:.1f} | P2 {p['avg_p2']:.1f} | P3 {p['avg_p3']:.1f})\n"
        )
    await m.answer("\n".join(lines), parse_mode="Markdown")


@router.message(Command("player"))
async def cmd_player(m: Message):
    if not await ensure_pool():
        await m.answer("❌ БД недоступна")
        return
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        await m.answer("Формат: `/player sPectacleee`", parse_mode="Markdown")
        return
    nick = parts[1].strip()
    p = await get_player_by_nick(nick)
    if not p:
        await m.answer(f"Игрок `{nick}` не найден.", parse_mode="Markdown")
        return

    lines = [
        f"👤 *{p['nick']}*\n",
        f"Матчей: *{p['matches']}*",
        f"🏅 Побед: *{p['wins']}* | Поражений: *{p['losses']}* | Ничьих: *{p['draws']}*",
        f"📈 % побед: *{p['win_pct']:.1f}%*\n",
        f"⚽ Ср. забито: *{p['avg_for']:.2f}*",
        f"🥅 Ср. пропущено: *{p['avg_against']:.2f}*",
        f"📊 Ср. общий тотал: *{p['avg_total']:.2f}*\n",
        f"P1: *{p['avg_p1']:.2f}*",
        f"P2: *{p['avg_p2']:.2f}*",
        f"P3: *{p['avg_p3']:.2f}*\n",
        f"Всего забито: *{p['total_goals']}*",
    ]
    await m.answer("\n".join(lines), parse_mode="Markdown")


@router.message()
async def any_message(m: Message):
    if m.photo:
        await m.answer("📸 Распознаю...")
        photo = m.photo[-1]
        f = await m.bot.get_file(photo.file_id)
        buf = await m.bot.download_file(f.file_path)
        try:
            data = buf.getvalue()
        except AttributeError:
            data = bytes(buf.read())
        data = prepare_image(data)
        text = await ocr_image(data)
        if not text:
            await m.answer("❌ OCR не смог распознать.")
            return
        matches = parse_block(text, default_league="Общая")
        if not matches:
            await m.answer("⚠️ Матчей не найдено.")
            return
        if not await ensure_pool():
            await m.answer("❌ БД недоступна")
            return
        saved = 0
        for mt in matches:
            try:
                await insert_match(**mt)
                saved += 1
            except Exception as e:
                log.error(f"insert_match failed: {e}")
        await m.answer(f"✅ Добавлено {saved}/{len(matches)} матчей.")
        return

    if m.text:
        matches = parse_block(m.text)
        if not matches:
            await m.answer(
                "🤔 Не понял. Формат:\n"
                "`[2026-09-10] [Лига]`\n"
                "`Команда1 - Команда2 3:2 (1:1,1:0,1:1)`",
                parse_mode="Markdown"
            )
            return
        if not await ensure_pool():
            await m.answer("❌ БД недоступна")
            return
        saved = 0
        for mt in matches:
            try:
                await insert_match(**mt)
                saved += 1
            except Exception as e:
                log.error(f"insert_match failed: {e}")
        lg = set(x["league"] for x in matches)
        dates = set(str(x["match_date"]) for x in matches if x["match_date"])
        txt = f"✅ Добавлено {saved}/{len(matches)} матчей.\nЛиги: {', '.join(lg)}"
        if dates:
            txt += f"\nДаты: {', '.join(dates)}"
        await m.answer(txt)


# ---------- WEB ----------
async def health(_):
    return web.Response(text="OK")


async def start_web():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(f"=== WEB SERVER ON :{port} ===")


async def main():
    await start_web()
    asyncio.create_task(_db_bootstrap())

    if not BOT_TOKEN:
        log.error("=== BOT_TOKEN not set! ===")
        return

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("=== WEBHOOK CLEARED ===")
    except Exception as e:
        log.warning(f"delete_webhook failed: {e}")

    log.info("=== BOT POLLING ===")
    await dp.start_polling(bot)


async def _db_bootstrap():
    await create_pool_with_retry(max_attempts=100, delay=15)
    if pool is not None:
        await init_db()


if __name__ == "__main__":
    asyncio.run(main())