import asyncio
import logging
import os
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')
DATABASE_PATH = os.getenv('DATABASE_PATH', 'shabbat_bot.db')
CHECK_INTERVAL_SECONDS = int(os.getenv('CHECK_INTERVAL_SECONDS', '30'))

if not BOT_TOKEN:
    raise RuntimeError('В .env не задан BOT_TOKEN')

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(name)s | %(message)s')
router = Router()

MONTHS_RU = {1:'января',2:'февраля',3:'марта',4:'апреля',5:'мая',6:'июня',7:'июля',8:'августа',9:'сентября',10:'октября',11:'ноября',12:'декабря'}

class Registration(StatesGroup):
    waiting_for_city = State()

async def init_db():
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            city TEXT NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            timezone TEXT NOT NULL,
            country_code TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            last_friday_sent_date TEXT,
            last_sunday_sent_date TEXT
        )''')
        await db.commit()

async def save_user(telegram_id, city, latitude, longitude, timezone, country_code):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute('''INSERT INTO users
        (telegram_id,city,latitude,longitude,timezone,country_code,enabled,last_friday_sent_date,last_sunday_sent_date)
        VALUES (?,?,?,?,?,?,1,NULL,NULL)
        ON CONFLICT(telegram_id) DO UPDATE SET
          city=excluded.city, latitude=excluded.latitude, longitude=excluded.longitude,
          timezone=excluded.timezone, country_code=excluded.country_code, enabled=1,
          last_friday_sent_date=NULL, last_sunday_sent_date=NULL''',
          (telegram_id, city, latitude, longitude, timezone, country_code))
        await db.commit()

async def get_user(telegram_id):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute('SELECT * FROM users WHERE telegram_id=?', (telegram_id,))
        return await cur.fetchone()

async def get_enabled_users():
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute('SELECT * FROM users WHERE enabled=1')
        return await cur.fetchall()

async def mark_sent(telegram_id, column, value):
    if column not in {'last_friday_sent_date','last_sunday_sent_date'}:
        return
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(f'UPDATE users SET {column}=? WHERE telegram_id=?', (value, telegram_id))
        await db.commit()

async def geocode_city(city):
    url = 'https://geocoding-api.open-meteo.com/v1/search'
    params = {'name':city,'count':1,'language':'ru','format':'json'}
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, params=params) as r:
            r.raise_for_status(); data = await r.json()
    results = data.get('results') or []
    if not results: return None
    p = results[0]
    parts = [p['name']]
    if p.get('admin1') and p['admin1'].lower() != p['name'].lower(): parts.append(p['admin1'])
    if p.get('country'): parts.append(p['country'])
    return {'city':', '.join(parts),'latitude':p['latitude'],'longitude':p['longitude'],
            'timezone':p['timezone'],'country_code':p.get('country_code')}

def iso_time(value):
    if not value or 'T' not in value: return None
    try: return datetime.fromisoformat(value).strftime('%H:%M')
    except ValueError: return None

def ru_date(value):
    d = date.fromisoformat(value[:10]); return f'{d.day} {MONTHS_RU[d.month]}'

def clean_title(title):
    title = re.sub(r'^(Эрев|Erev)\s+', '', title, flags=re.I)
    title = re.sub(r'\s+[IVX]+$', '', title)
    return title.strip()

async def hebcal(user, start, end, parasha=False):
    params = {
        'cfg':'json','v':'1','start':start.isoformat(),'end':end.isoformat(),
        'geo':'pos','latitude':str(user['latitude']),'longitude':str(user['longitude']),
        'tzid':user['timezone'],'lg':'ru','c':'on','M':'on','maj':'on','min':'on','mf':'on','nx':'on',
        'leyning':'off','i':'on' if (user['country_code'] or '').upper()=='IL' else 'off'
    }
    if parasha: params['s']='on'
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get('https://www.hebcal.com/hebcal', params=params) as r:
            r.raise_for_status(); return await r.json()

def next_friday(today):
    friday = today + timedelta(days=(4-today.weekday())%7)
    return friday, friday+timedelta(days=1)

async def shabbat_info(user):
    local = datetime.now(ZoneInfo(user['timezone']))
    friday, saturday = next_friday(local.date())
    data = await hebcal(user, friday, saturday, parasha=True)
    candles = havdalah = parasha = None
    for item in data.get('items',[]):
        cat = item.get('category')
        if cat=='candles' and not candles: candles=iso_time(item.get('date'))
        elif cat=='havdalah': havdalah=iso_time(item.get('date'))
        elif cat=='parashat' and not parasha:
            parasha=item.get('title','').replace('Парашат ','').strip()
    return {'friday':friday,'candles':candles,'havdalah':havdalah,'parasha':parasha}

def shabbat_text(user, info, automatic=False):
    if automatic:
        header='🕯 <b>Шаббат на этой неделе</b>'
    else:
        header=f"🕯 На этой неделе Шаббат начнётся <b>{ru_date(info['friday'].isoformat())}</b>."
    return (f"{header}\n\n"
            f"Зажигание свечей / начало: <b>{info['candles'] or 'нет данных'}</b>\n"
            f"Исход Шаббата: <b>{info['havdalah'] or 'нет данных'}</b>\n"
            f"Глава недели: <b>{info['parasha'] or 'нет данных'}</b>\n\n"
            f"📍 {user['city']}\n<i>Времена: Hebcal.com</i>")

def relevant(item):
    if item.get('category')=='roshchodesh': return True
    return item.get('category')=='holiday' and item.get('subcat') in {'major','minor','fast'}

async def upcoming_events(user, count=3):
    today = datetime.now(ZoneInfo(user['timezone'])).date()
    data = await hebcal(user, today, today+timedelta(days=180), parasha=False)
    items = data.get('items',[])
    candles = {}
    for item in items:
        if item.get('category')=='candles':
            ds=item.get('date','')[:10]; t=iso_time(item.get('date'))
            if ds and t: candles[ds]=t
    out=[]; seen=set()
    for item in items:
        if not relevant(item): continue
        raw=item.get('title','').strip(); ds=item.get('date','')[:10]
        if not raw or not ds: continue
        title=clean_title(raw); key=title.lower()
        if key in seen: continue
        seen.add(key)
        start_time=None
        if raw.lower().startswith(('эрев ','erev ')):
            start_time=candles.get(ds)
        elif item.get('subcat')=='major':
            prev=(date.fromisoformat(ds)-timedelta(days=1)).isoformat(); start_time=candles.get(prev)
        out.append({'title':title,'date':ds,'start_time':start_time})
    out.sort(key=lambda x:x['date'])
    return out[:count]

def events_text(events, automatic=False):
    if not events: return 'Не удалось найти ближайшие события.'
    lines=[]
    for e in events:
        extra=f", начало в {e['start_time']}" if e['start_time'] else ''
        lines.append(f"• <b>{e['title']}</b> — {ru_date(e['date'])}{extra}")
    header='✨ <b>Хорошей недели!</b>\n\nБлижайшие события:' if automatic else '✡️ <b>Ближайшие события:</b>'
    return header+'\n'+'\n'.join(lines)+'\n\n<i>Календарь: Hebcal.com</i>'

@router.message(CommandStart())
async def start(message:Message,state:FSMContext):
    await state.clear(); await state.set_state(Registration.waiting_for_city)
    await message.answer('Шалом! ✡️\n\nЯ буду напоминать о времени Шаббата и ближайших событиях еврейского календаря.\n\nДля начала напиши свой город.')

@router.message(Command('city'))
async def city(message:Message,state:FSMContext):
    await state.clear(); await state.set_state(Registration.waiting_for_city); await message.answer('Напиши новый город:')

@router.message(Command('shabbat'))
async def shabbat_cmd(message:Message):
    user=await get_user(message.from_user.id)
    if not user: return await message.answer('Сначала настрой город командой /start.')
    try: await message.answer(shabbat_text(user, await shabbat_info(user), False))
    except Exception:
        logging.exception('Shabbat error'); await message.answer('Не получилось получить данные. Попробуй чуть позже.')

@router.message(Command('holidays'))
async def holidays_cmd(message:Message):
    user=await get_user(message.from_user.id)
    if not user: return await message.answer('Сначала настрой город командой /start.')
    try: await message.answer(events_text(await upcoming_events(user,5), False))
    except Exception:
        logging.exception('Holiday error'); await message.answer('Не получилось получить календарь. Попробуй чуть позже.')

@router.message(Command('help'))
async def help_cmd(message:Message):
    await message.answer('<b>Что я умею:</b>\n\n/shabbat — ближайший Шаббат\n/holidays — ближайшие события\n/city — сменить город\n\nАвтоматические сводки: пятница и воскресенье в 09:00 по местному времени.')

@router.message(Registration.waiting_for_city, F.text)
async def receive_city(message:Message,state:FSMContext):
    try: place=await geocode_city(message.text.strip())
    except Exception:
        logging.exception('Geocoding error'); return await message.answer('Сервис городов временно не ответил. Попробуй ещё раз.')
    if not place: return await message.answer('Не нашёл такой город. Попробуй написать точнее.')
    try: ZoneInfo(place['timezone'])
    except ZoneInfoNotFoundError: return await message.answer('Не смог определить часовой пояс. Укажи город и страну.')
    await save_user(message.from_user.id, place['city'], place['latitude'], place['longitude'], place['timezone'], place['country_code'])
    await state.clear()
    await message.answer(f"Готово ✡️\n\nГород: <b>{place['city']}</b>\n\nПятница 09:00 — время Шаббата, свечей, исхода и глава недели.\nВоскресенье 09:00 — три ближайших события.\n\n/shabbat — проверить сейчас\n/holidays — ближайшие события\n/city — сменить город")

async def worker(bot:Bot):
    while True:
        try:
            users=await get_enabled_users(); now_utc=datetime.now(ZoneInfo('UTC'))
            for user in users:
                try:
                    local=now_utc.astimezone(ZoneInfo(user['timezone'])); ds=local.date().isoformat()
                    if local.weekday()==4 and local.hour==9 and user['last_friday_sent_date']!=ds:
                        await bot.send_message(user['telegram_id'], shabbat_text(user, await shabbat_info(user), True))
                        await mark_sent(user['telegram_id'],'last_friday_sent_date',ds)
                    if local.weekday()==6 and local.hour==9 and user['last_sunday_sent_date']!=ds:
                        await bot.send_message(user['telegram_id'], events_text(await upcoming_events(user,3), True))
                        await mark_sent(user['telegram_id'],'last_sunday_sent_date',ds)
                except Exception: logging.exception('Background user error %s', user['telegram_id'])
        except Exception: logging.exception('Background loop error')
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)

async def main():
    await init_db()
    bot=Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp=Dispatcher(storage=MemoryStorage()); dp.include_router(router)
    task=asyncio.create_task(worker(bot))
    try: await dp.start_polling(bot)
    finally:
        task.cancel(); await bot.session.close()

if __name__=='__main__': asyncio.run(main())
