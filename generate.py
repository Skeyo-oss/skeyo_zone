import os
import re
import json
import html
import datetime as dt
from zoneinfo import ZoneInfo

import requests
import feedparser
import anthropic
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

TZ = ZoneInfo("Europe/Warsaw")
NOW = dt.datetime.now(TZ)

# Lokalizacja pogody (Łódź). Zmień, jeśli potrzeba.
LAT, LON = 51.75, 19.46

MODEL = "claude-haiku-4-5"

NEWS_FEEDS = [
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://tvn24.pl/najnowsze.xml",
]

DAYS = ["Poniedziałek", "Wtorek", "Środa", "Czwartek", "Piątek", "Sobota", "Niedziela"]
MONTHS = ["stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca", "lipca",
          "sierpnia", "września", "października", "listopada", "grudnia"]


def safe(fn, default):
    try:
        return fn()
    except Exception as e:
        print(f"[błąd] {fn.__name__}: {e}")
        return default


def get_weather():
    r = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": LAT,
            "longitude": LON,
            "timezone": "Europe/Warsaw",
            "forecast_days": 1,
            "models": "icon_seamless,gfs_seamless,ecmwf_ifs025,meteofrance_seamless",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,"
                     "precipitation_probability_max,wind_gusts_10m_max,weather_code",
            "hourly": "precipitation,weather_code",
        },
        timeout=30,
    )
    r.raise_for_status()
    j = r.json()
    return {"daily": j["daily"], "hourly": j["hourly"]}


def get_crypto():
    r = requests.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": "bitcoin,ethereum", "vs_currencies": "usd",
                "include_24hr_change": "true"},
        timeout=30,
    )
    r.raise_for_status()
    d = r.json()
    return {
        "BTC": {"usd": d["bitcoin"]["usd"], "zmiana": d["bitcoin"]["usd_24h_change"]},
        "ETH": {"usd": d["ethereum"]["usd"], "zmiana": d["ethereum"]["usd_24h_change"]},
    }


def get_news():
    titles = []
    for url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:12]:
                titles.append(e.get("title", ""))
        except Exception as ex:
            print(f"[błąd] news {url}: {ex}")
    return titles


def google_creds():
    return Credentials(
        None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        scopes=[
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/calendar.readonly",
        ],
    )


def get_calendar():
    svc = build("calendar", "v3", credentials=google_creds(), cache_discovery=False)
    start = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(days=1)
    items = svc.events().list(
        calendarId="primary",
        timeMin=start.isoformat(),
        timeMax=end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=25,
    ).execute().get("items", [])
    out = []
    for e in items:
        out.append({
            "start": e["start"].get("dateTime", e["start"].get("date")),
            "tytul": e.get("summary", "(bez tytułu)"),
            "miejsce": e.get("location", ""),
        })
    return out


def get_mail():
    svc = build("gmail", "v1", credentials=google_creds(), cache_discovery=False)
    q = "newer_than:2d in:inbox -category:promotions -category:social -category:forums"
    msgs = svc.users().messages().list(userId="me", q=q, maxResults=40).execute().get("messages", [])
    out = []
    for m in msgs:
        msg = svc.users().messages().get(
            userId="me", id=m["id"], format="metadata",
            metadataHeaders=["From", "Subject"],
        ).execute()
        h = {x["name"]: x["value"] for x in msg["payload"]["headers"]}
        out.append({
            "od": h.get("From", ""),
            "temat": h.get("Subject", ""),
            "fragment": msg.get("snippet", "")[:200],
        })
    return out


PROMPT = """Jesteś asystentem, który układa poranny ekran powitalny. Dziś: {date}.
Odpowiadasz po polsku, krótko, konkretnie i neutralnie. Zwróć WYŁĄCZNIE poprawny JSON (bez markdown, bez komentarza) w formacie:

{{
  "pogoda": "1-2 zdania: temperatura rano i w ciągu dnia, opady/burze i o której godzinie, czy brać parasol. Porównaj modele; jeśli się różnią, napisz to jednym zdaniem.",
  "dzis": ["punkt z kalendarza z godziną", "..."],
  "poczta": ["kto i o co, jednym zdaniem", "..."],
  "poczta_pominieto": 0,
  "swiat": ["jedno zdanie", "..."]
}}

Zasady:
- dzis: przypomnienia z kalendarza na dziś, po jednym na linię, w kolejności godzin. Jeśli brak: ["Brak wydarzeń w kalendarzu"].
- poczta: tylko naprawdę ważne (faktury, płatności, dokumenty, urzędy, banki, terminy, sprawy osobiste i wiadomości od konkretnych osób). Pomiń newslettery, reklamy, powiadomienia platform i social mediów. Maksymalnie 5 pozycji. Jeśli brak: ["Brak ważnych wiadomości"]. W poczta_pominieto wpisz liczbę pominiętych.
- swiat: 3-4 najważniejsze wiadomości ze świata i z Polski (polityka, gospodarka, bezpieczeństwo), uproszczone, po jednym zdaniu.
- Ceny kryptowalut wstawia skrypt, nie umieszczaj ich w JSON.

DANE:
pogoda (modele: icon, gfs, ecmwf, meteofrance): {weather}
kalendarz: {calendar}
poczta: {mail}
nagłówki wiadomości: {news}
"""


def summarize(weather, calendar, mail, news):
    client = anthropic.Anthropic()
    prompt = PROMPT.format(
        date=f"{DAYS[NOW.weekday()]} {NOW.day} {MONTHS[NOW.month - 1]} {NOW.year}",
        weather=json.dumps(weather, ensure_ascii=False),
        calendar=json.dumps(calendar, ensure_ascii=False),
        mail=json.dumps(mail, ensure_ascii=False),
        news=json.dumps(news, ensure_ascii=False),
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    return json.loads(text)


def fmt_price(v):
    return f"{v:,.0f}".replace(",", " ")


def fmt_change(c):
    return f"{c:+.1f}%".replace(".", ",").replace("-", "−")


CSS = """
*{box-sizing:border-box}
html,body{margin:0;background:#000}
body{font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;color:#b5b5b5;
max-width:420px;margin:0 auto;padding:26px 22px 40px}
.top{display:flex;justify-content:space-between;font-size:12px;color:#5a5a5a;letter-spacing:.5px}
.day{font-size:28px;font-weight:500;color:#e4e4e4;line-height:1.2;margin-top:18px}
.date{font-size:14px;color:#6e6e6e;margin:2px 0 20px}
.sec{border-top:.5px solid #262626;padding:14px 0}
.lab{font-size:11px;color:#555;margin-bottom:6px}
.t{font-size:15px;color:#e4e4e4;line-height:1.5}
.t2{font-size:15px;color:#b5b5b5;line-height:1.5}
.s{font-size:12px;color:#6e6e6e;margin-top:4px}
.row{display:flex;gap:24px;flex-wrap:wrap;font-size:15px;color:#e4e4e4;margin-bottom:8px}
.row small{color:#6e6e6e;font-size:12px}
"""


def esc(s):
    return html.escape(str(s))


def lines(items):
    out = []
    for i, it in enumerate(items):
        cls = "t" if i == 0 else "t2"
        out.append(f'<div class="{cls}">{esc(it)}</div>')
    return "\n".join(out)


def render(ai, crypto):
    day = DAYS[NOW.weekday()]
    date = f"{NOW.day} {MONTHS[NOW.month - 1]}"
    mail_n = ai.get("poczta", [])
    skipped = ai.get("poczta_pominieto", 0)

    parts = [
        f'<div class="top"><span>Skeyoo_zone</span><span>{NOW:%H:%M}</span></div>',
        f'<div class="day">{esc(day)}</div><div class="date">{esc(date)}</div>',
        f'<div class="sec"><div class="lab">Pogoda</div><div class="t">{esc(ai.get("pogoda", "Brak danych"))}</div></div>',
        f'<div class="sec"><div class="lab">Dziś</div>{lines(ai.get("dzis", ["Brak danych"]))}</div>',
        f'<div class="sec"><div class="lab">Poczta</div>{lines(mail_n)}'
        + (f'<div class="s">Pominięto {esc(skipped)} mniej ważnych</div>' if skipped else "")
        + "</div>",
    ]

    crypto_html = ""
    if crypto:
        crypto_html = '<div class="row">' + "".join(
            f'<span>{k} {fmt_price(v["usd"])} <small>{fmt_change(v["zmiana"])}</small></span>'
            for k, v in crypto.items()
        ) + "</div>"
    parts.append(
        f'<div class="sec"><div class="lab">Świat</div>{crypto_html}{lines(ai.get("swiat", ["Brak danych"]))}</div>'
    )

    return (
        '<!doctype html><html lang="pl"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Skeyoo_zone</title>'
        f'<style>{CSS}</style></head><body>' + "\n".join(parts) + "</body></html>"
    )


def main():
    weather = safe(get_weather, {})
    calendar = safe(get_calendar, [])
    mail = safe(get_mail, [])
    news = safe(get_news, [])
    crypto = safe(get_crypto, {})

    try:
        ai = summarize(weather, calendar, mail, news)
    except Exception as e:
        print(f"[błąd] streszczenie: {e}")
        ai = {"pogoda": "Nie udało się przygotować podsumowania.", "dzis": [], "poczta": [], "swiat": []}

    os.makedirs("site", exist_ok=True)
    with open("site/index.html", "w", encoding="utf-8") as f:
        f.write(render(ai, crypto))
    print("Gotowe: site/index.html")


if __name__ == "__main__":
    main()
