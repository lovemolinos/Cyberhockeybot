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
delete_pending = {}


# ---------- POISSON ----------
def poisson_pmf(k, lam):
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def poisson_cdf(k, lam):
    return sum(poisson_pmf(i, lam) for i in range(int(k) + 1))


def over_prob(lam, threshold):
    return 1.0 - poisson_cdf(int(threshold), lam)


# ---------- DATABASE ----------
async def create_pool_with_retry(max_attempts=100, delay=10):
    global pool
    if not DATABASE_URL:
        log.error("=== DATABASE_URL not set! ===")
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
                CREATE INDEX IF NOT EXISTS idx_team1 ON matches(team1);
                CREATE INDEX IF NOT EXISTS idx_team2 ON matches(team2);
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
        except Exception as e:
            log.warning(f"Pool broken: {e}. Reconnecting...")
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


async def get_setting(key, default=None):
    if not await ensure_pool():
        return default
    async with pool.acquire() as c:
        r = await c.fetchrow("SELECT value FROM settings WHERE key=$1", key)
        return r["value"] if r else default


async def set_setting(key, value):
    if not await ensure_pool():
        return False
    async with pool.acquire() as c:
        await c.execute("""
            INSERT INTO settings (key, value) VALUES ($1, $2)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, key, str(value))
    return True


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


# ---------- IMAGE PREP ----------
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
    except Exception as e:
        log.error(f"prepare_image failed: {e}")
        return image_bytes


# ---------- OCR ----------
async def ocr_image(image_bytes):
    if not OCR_API_KEY:
        log.error("OCR_API_KEY не задан")
        return None
    try:
        log.info(f"OCR: отправляю {len(image_bytes)} байт")
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
                log.info(f"OCR HTTP статус: {r.status}")
                j = await r.json()
        if j.get("IsErroredOnProcessing"):
            log.error(f"OCR ошибка: {j.get('ErrorMessage')}")
            return None
        results = j.get("ParsedResults")
        if not results:
            return None
        return results[0].get("ParsedText", "")
    except Exception as e:
        log.error(f"OCR исключение: {e}", exc_info=True)
        return None


# ---------- STATS ----------
async def team_stats(team, league=None):
    if not await ensure_pool():
        return None
    q = """SELECT COUNT(*) AS n,
        AVG(total)::float AS avg_total,
        AVG(p1_total)::float AS avg_p1,
        AVG(p2_total)::float AS avg_p2,
        AVG(p3_total)::float AS avg_p3
        FROM matches WHERE (team1=$1 OR team2=$1)"""
    args = [team]
    if league:
        q += " AND league=$2"
        args.append(league)
    async with pool.acquire() as c:
        r = await c.fetchrow(q, *args)
    if r and r["n"] and r["n"] > 0:
        return dict(r)
    return None


async def league_stats(league):
    if not await ensure_pool():
        return None
    async with pool.acquire() as c:
        r = await c.fetchrow("""
            SELECT COUNT(*) AS n,
                AVG(total)::float AS avg_total,
                AVG(p1_total)::float AS avg_p1,
                AVG(p2_total)::float AS avg_p2,
                AVG(p3_total)::float AS avg_p3
            FROM matches WHERE league=$1
        """, league)
    if r and r["n"] and r["n"] > 0:
        return dict(r)
    return None


async def list_leagues():
    if not await ensure_pool():
        return []
    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT DISTINCT league FROM matches ORDER BY league"
        )
    return [r["league"] for r in rows]


async def teams_in_league(league):
    if not await ensure_pool():
        return []
    async with pool.acquire() as c:
        rows = await c.fetch("""
            SELECT DISTINCT team FROM (
                SELECT team1 AS team FROM matches WHERE league=$1
                UNION
                SELECT team2 FROM matches WHERE league=$1
            ) t ORDER BY team
        """, league)
    return [r["team"] for r in rows]


async def find_league(t1, t2):
    if not await ensure_pool():
        return None
    async with pool.acquire() as c:
        r = await c.fetchrow("""
            SELECT league FROM matches
            WHERE (team1=$1 AND team2=$2) OR (team1=$2 AND team2=$1)
            GROUP BY league ORDER BY COUNT(*) DESC LIMIT 1
        """, t1, t2)
    return r["league"] if r else None


# ---------- PREDICT ----------
async def predict(league, t1, t2, odds):
    s1 = await team_stats(t1, league)
    s2 = await team_stats(t2, league)
    ls = await league_stats(league)
    if not s1 or not s2 or not ls:
        return None
    exp_total = (
        s1["avg_total"] * 0.4
        + s2["avg_total"] * 0.4
        + ls["avg_total"] * 0.2
    )
    exp_p = []
    for k in ("avg_p1", "avg_p2", "avg_p3"):
        val = (
            (s1[k] or 0) * 0.4
            + (s2[k] or 0) * 0.4
            + (ls[k] or 0) * 0.2
        )
        exp_p.append(val)
    probs = {
        "ТБ 4.5 матч": over_prob(exp_total, 4.5),
        "ТБ 5.5 матч": over_prob(exp_total, 5.5),
        "ТБ 6.5 матч": over_prob(exp_total, 6.5),
        "ТБ 1.5 P1": over_prob(exp_p[0], 1.5),
        "ТБ 1.5 P2": over_prob(exp_p[1], 1.5),
        "ТБ 1.5 P3": over_prob(exp_p[2], 1.5),
    }
    best_name, best_prob = max(
        probs.items(), key=lambda kv: kv[1] * odds - 1
    )
    value = best_prob * odds - 1
    return {
        "league": league,
        "t1": t1,
        "t2": t2,
        "exp_total": round(exp_total, 2),
        "exp_p": [round(x, 2) for x in exp_p],
        "probs": probs,
        "odds": odds,
        "best_name": best_name,
        "best_prob": best_prob,
        "value": value,
    }


# ---------- HANDLERS ----------
@router.message(Command("start"))
async def cmd_start(m: Message):
    await m.answer(
        "🏒 *Cyber Hockey Bot*\n\n"
        "📷 Отправь скриншот Фонбет\n"
        "📝 Или текст:\n"
        "`[2026-09-10] [Лига]`\n"
        "`Команда1 - Команда2 3:2 (1:1,1:0,1:1)`\n\n"
        "*Прогноз:* `/predict Лига: Т1 - Т2 1.85`\n"
        "*Статистика:* /leagues /stats /by_date\n"
        "*Банк:* /bank 10000\n"
        "*Управление:* /delete_all /db",
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
        await m.answer("❌ Пул не создан. Пробую переподключиться...")
        ok = await ensure_pool()
        if ok:
            await m.answer("✅ Переподключился.")
        else:
            await m.answer("❌ Не удалось. Смотри логи Render.")
        return
    try:
        async with pool.acquire() as c:
            r = await c.fetchrow("SELECT COUNT(*) AS n FROM matches")
        await m.answer(f"✅ БД работает. Матчей: {r['n']}")
    except Exception as e:
        await m.answer(f"❌ Ошибка: {e}")


@router.message(Command("delete_all"))
async def cmd_delete_all(m: Message):
    parts = m.text.split(maxsplit=1)
    if len(parts) == 2 and parts[1].strip().lower() == "yes":
        if not await ensure_pool():
            await m.answer("❌ БД недоступна, попробуй позже")
            return
        async with pool.acquire() as c:
            await c.execute("DELETE FROM matches")
        delete_pending.pop(m.from_user.id, None)
        await m.answer("🗑 Все матчи удалены.")
        return
    delete_pending[m.from_user.id] = True
    await m.answer(
        "⚠️ *Ты уверен?* Это удалит ВСЕ матчи.\n\n"
        "Напиши `/delete_all yes` для подтверждения.",
        parse_mode="Markdown"
    )


@router.message(Command("by_date"))
async def cmd_by_date(m: Message):
    if not await ensure_pool():
        await m.answer("❌ БД недоступна")
        return
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        await m.answer(
            "Формат: `/by_date 2026-09-10`\n"
            "или: `/by_date 2026-09-01 2026-09-10`",
            parse_mode="Markdown"
        )
        return
    args = parts[1].split()
    try:
        d_from = datetime.strptime(args[0].replace(".", "-"), "%Y-%m-%d").date()
        d_to = (
            datetime.strptime(args[1].replace(".", "-"), "%Y-%m-%d").date()
            if len(args) > 1 else d_from
        )
    except Exception:
        await m.answer("Не понял дату. Формат: `2026-09-10`",
                       parse_mode="Markdown")
        return
    async with pool.acquire() as c:
        r = await c.fetchrow("""
            SELECT COUNT(*) AS n,
                AVG(total)::float AS avg_total,
                AVG(p1_total)::float AS avg_p1,
                AVG(p2_total)::float AS avg_p2,
                AVG(p3_total)::float AS avg_p3
            FROM matches
            WHERE match_date BETWEEN $1 AND $2
        """, d_from, d_to)
    if not r or not r["n"]:
        await m.answer("За этот период нет матчей.")
        return
    await m.answer(
        f"📅 *{d_from} — {d_to}*\n"
        f"Матчей: {r['n']}\n"
        f"Ср. тотал: {(r['avg_total'] or 0):.2f}\n"
        f"Ср. P1: {(r['avg_p1'] or 0):.2f}\n"
        f"Ср. P2: {(r['avg_p2'] or 0):.2f}\n"
        f"Ср. P3: {(r['avg_p3'] or 0):.2f}",
        parse_mode="Markdown"
    )


@router.message(Command("leagues"))
async def cmd_leagues(m: Message):
    ls = await list_leagues()
    if not ls:
        await m.answer("Пока нет данных.")
        return
    lines = [f"• {x}" for x in ls]
    await m.answer("🏙 *Лиги:*\n" + "\n".join(lines), parse_mode="Markdown")


@router.message(Command("teams"))
async def cmd_teams(m: Message):
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        await m.answer("Формат: /teams Лига")
        return
    teams = await teams_in_league(parts[1].strip())
    if not teams:
        await m.answer("Лига не найдена или пуста")
        return
    txt = "\n".join(f"• {t}" for t in teams)
    await m.answer(f"🏒 *{parts[1].strip()}*:\n{txt}",
                   parse_mode="Markdown")


@router.message(Command("stats"))
async def cmd_stats(m: Message):
    parts = m.text.split(maxsplit=2)
    if len(parts) < 2:
        await m.answer("Формат: /stats Команда [Лига]")
        return
    team = parts[1].strip()
    league = parts[2].strip() if len(parts) > 2 else None
    s = await team_stats(team, league)
    if not s:
        await m.answer("Нет данных по команде")
        return
    title = f"📊 *{team}*"
    if league:
        title += f" ({league})"
    await m.answer(
        title + "\n"
        f"Матчей: {s['n']}\n"
        f"Ср. тотал матча: {(s['avg_total'] or 0):.2f}\n"
        f"Ср. P1: {(s['avg_p1'] or 0):.2f}\n"
        f"Ср. P2: {(s['avg_p2'] or 0):.2f}\n"
        f"Ср. P3: {(s['avg_p3'] or 0):.2f}",
        parse_mode="Markdown"
    )


@router.message(Command("bank"))
async def cmd_bank(m: Message):
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        cur = await get_setting("bank", "10000")
        await m.answer(f"Текущий банк: {cur} ₽\nИзменить: /bank 5000")
        return
    try:
        v = float(parts[1].replace(",", ".").replace(" ", ""))
        ok = await set_setting("bank", v)
        if ok:
            await m.answer(f"✅ Банк установлен: {v:.0f} ₽")
        else:
            await m.answer("❌ БД недоступна")
    except ValueError:
        await m.answer("Введите число, например: /bank 5000")


@router.message(Command("predict"))
async def cmd_predict(m: Message):
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        await m.answer("Формат: /predict Лига: Т1 - Т2 1.85")
        return
    arg = parts[1].strip()
    league = None
    pref = re.match(r'^([^:\d]{2,40})\s*:\s*(.+)$', arg)
    if pref and re.search(r'[-–—]', pref.group(2)):
        league = pref.group(1).strip()
        arg = pref.group(2).strip()
    odds = 1.85
    om = re.search(r'(\d+[.,]\d+|\d+)\s*$', arg)
    if om:
        try:
            odds = float(om.group(1).replace(",", "."))
            arg = arg[:om.start()].strip()
        except ValueError:
            pass
    tm = re.split(r'\s+[-–—]\s+', arg, maxsplit=1)
    if len(tm) != 2:
        await m.answer("Формат: /predict Лига: Т1 - Т2 1.85")
        return
    t1, t2 = tm[0].strip(), tm[1].strip()
    if not league:
        league = await find_league(t1, t2)
    if not league:
        await m.answer(
            "Не нашёл лигу. Укажите вручную:\n"
            "/predict Лига: Команда1 - Команда2 1.85"
        )
        return
    p = await predict(league, t1, t2, odds)
    if not p:
        await m.answer("Недостаточно данных.")
        return
    lines = [
        f"🏒 *{t1} vs {t2}* ({league})",
        "",
        f"📊 Ожидаемый тотал: *{p['exp_total']}*",
        f"⏱ P1: {p['exp_p'][0]} | P2: {p['exp_p'][1]} | P3: {p['exp_p'][2]}",
        "",
        "*Вероятности:*",
    ]
    for k, v in p["probs"].items():
        lines.append(f"  • {k}: {v*100:.1f}%")
    lines.append("")
    if p["value"] > 0.05:
        bank = float(await get_setting("bank", "10000"))
        b = odds - 1
        if b > 0:
            kelly = (b * p["best_prob"] - (1 - p["best_prob"])) / b
        else:
            kelly = 0
        stake = max(0, kelly * 0.25 * bank)
        lines += [
            f"✅ *VALUE BET:* {p['best_name']}",
            f"💵 Ставка: *{stake:.0f} ₽*",
            f"📈 Value: {p['value']*100:+.1f}%",
        ]
    else:
        lines.append(f"❌ Value = {p['value']*100:+.1f}%. Пропуск.")
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
            await m.answer("❌ OCR не смог распознать картинку.")
            return

        matches = parse_block(text, default_league="Общая")
        if not matches:
            preview = text[:1500]
            await m.answer(
                "⚠️ OCR распознал текст, но я не нашёл матчей.\n\n"
                f"`{preview}`",
                parse_mode="Markdown"
            )
            return

        if not await ensure_pool():
            await m.answer("❌ БД недоступна, попробуй через минуту")
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
            await m.answer("❌ БД недоступна, попробуй через минуту")
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


# ---------- MAIN ----------
async def main():
    await start_web()

    asyncio.create_task(_db_bootstrap())

    if not BOT_TOKEN:
        log.error("=== BOT_TOKEN not set! ===")
        return
    if not OCR_API_KEY:
        log.error("=== OCR_API_KEY not set! ===")

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