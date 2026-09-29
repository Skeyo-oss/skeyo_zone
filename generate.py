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

# Łaziska Górne
PLACE = "Łaziska Górne"
LAT, LON = 50.14, 18.60

MODEL = "claude-haiku-4-5"

NEWS_FEEDS = [
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://tvn24.pl/najnowsze.xml",
    "https://www.theguardian.com/world/rss",
    "https://www.aljazeera.com/xml/rss/all.xml",
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
            for e in feed.entries[:15]:
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


PROMPT = """Jesteś asystentem, który układa poranny ekran powitalny. Dziś: {date}. Miejsce: {place}.
Odpowiadasz po polsku, krótko, konkretnie i neutralnie. Zwróć WYŁĄCZNIE poprawny JSON (bez markdown, bez komentarza) w formacie:

{{
  "temperatura": "np. 11° / 17°  (rano / maksimum w dzień)",
  "pogoda": "1-2 zdania: opady/burze i o której godzinie, wiatr jeśli istotny, czy brać parasol. Porównaj modele; jeśli się różnią, napisz to jednym zdaniem.",
  "dzis": ["punkt z kalendarza zaczynający się od godziny, np. 16:30 Fryzjer", "..."],
  "poczta": ["kto i o co, jednym zdaniem", "..."],
  "poczta_pominieto": 0,
  "wiadomosci": [{{"kat": "Polska", "tekst": "jedno zdanie"}}, {{"kat": "Świat", "tekst": "jedno zdanie"}}]
}}

Zasady:
- dzis: przypomnienia z kalendarza na dziś, po jednym na linię, w kolejności godzin, każdy zaczyna się od godziny w formacie GG:MM (wydarzenie całodniowe bez godziny zaczynaj od słowa "Cały dzień"). Jeśli brak: ["Brak wydarzeń w kalendarzu"].
- poczta: tylko naprawdę ważne (faktury, płatności, dokumenty, urzędy, banki, terminy, sprawy osobiste i wiadomości od konkretnych osób). Pomiń newslettery, reklamy, powiadomienia platform i social mediów. Maksymalnie 5 pozycji. Jeśli brak: ["Brak ważnych wiadomości"]. W poczta_pominieto wpisz liczbę pominiętych.
- wiadomosci: 12-15 najważniejszych wiadomości z nagłówków, uproszczonych do jednego jasnego zdania. Pole "kat" to jedna z wartości: Polska, Świat, Gospodarka, Bezpieczeństwo, Technologia. Bez powtórzeń tego samego tematu.
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
        place=PLACE,
        weather=json.dumps(weather, ensure_ascii=False),
        calendar=json.dumps(calendar, ensure_ascii=False),
        mail=json.dumps(mail, ensure_ascii=False),
        news=json.dumps(news, ensure_ascii=False),
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=3500,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    return json.loads(text)


def esc(s):
    return html.escape(str(s))


def fmt_price(v):
    return f"{v:,.0f}".replace(",", " ")


def fmt_change(c):
    return f"{c:+.1f}%".replace(".", ",").replace("-", "−")


def plain_lines(items):
    return "".join(f'<div class="t">{esc(it)}</div>' for it in items)


def event_lines(items):
    out = []
    for it in items:
        m = re.match(r"^\s*(\d{1,2}:\d{2}|Cały dzień)\s*[-–:]?\s*(.*)$", str(it))
        if m:
            out.append(f'<div class="ev"><span class="h">{esc(m.group(1))}</span><span>{esc(m.group(2))}</span></div>')
        else:
            out.append(f'<div class="ev"><span class="h"></span><span>{esc(it)}</span></div>')
    return "".join(out)


def news_items(items):
    out = []
    for it in items:
        if isinstance(it, dict):
            kat, tekst = it.get("kat", ""), it.get("tekst", "")
        else:
            kat, tekst = "", it
        out.append(f'<div class="n"><small>{esc(kat)}</small>{esc(tekst)}</div>')
    return "".join(out)


CSS = r"""
:root{--t1:#ececec;--t2:#a8a8a8;--t3:#666;--t4:#3a3a3a;--line:#1c1c1c}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;height:100%;background:#000;overflow:hidden}
body{font-family:'Manrope',-apple-system,'Helvetica Neue',Arial,sans-serif;color:var(--t2);-webkit-font-smoothing:antialiased}
.deck{display:flex;height:100vh;height:100dvh;overflow-x:auto;overflow-y:hidden;scroll-snap-type:x mandatory;
scrollbar-width:none;-webkit-overflow-scrolling:touch;
background:radial-gradient(120% 55% at 50% -8%,#161616 0%,#000 62%)}
.deck::-webkit-scrollbar,.panel::-webkit-scrollbar{display:none}
.panel{flex:0 0 100%;width:100%;scroll-snap-align:start;scroll-snap-stop:always;overflow-y:auto;scrollbar-width:none;
padding:calc(env(safe-area-inset-top,0px) + 26px) 24px calc(env(safe-area-inset-bottom,0px) + 90px)}
.in{max-width:440px;margin:0 auto}
.in>*{opacity:0;transform:translateY(8px);animation:up .7s ease forwards}
.in>*:nth-child(2){animation-delay:.05s}.in>*:nth-child(3){animation-delay:.1s}
.in>*:nth-child(4){animation-delay:.15s}.in>*:nth-child(5){animation-delay:.2s}
.in>*:nth-child(6){animation-delay:.25s}.in>*:nth-child(7){animation-delay:.3s}
.in>*:nth-child(8){animation-delay:.35s}
@keyframes up{to{opacity:1;transform:none}}
.brand{display:flex;justify-content:space-between;font-size:12px;letter-spacing:2px;color:var(--t3)}
.brand b{font-weight:500;color:var(--t2);letter-spacing:3px}
.hello{font-size:13px;color:var(--t3);margin-top:34px;letter-spacing:.5px}
.day{font-size:46px;font-weight:200;color:var(--t1);letter-spacing:-1px;line-height:1.05;margin-top:6px}
.date{font-size:15px;color:var(--t3);margin:6px 0 30px;font-weight:300}
.sec{border-top:1px solid var(--line);padding:18px 0}
.lab{font-size:10.5px;letter-spacing:2.2px;text-transform:uppercase;color:var(--t3);margin-bottom:12px}
.temp{font-size:34px;font-weight:200;color:var(--t1);margin-bottom:6px;letter-spacing:-.5px}
.t{font-size:15px;line-height:1.55;color:var(--t1);font-weight:400;padding:2px 0}
.t2{font-size:14.5px;line-height:1.55;color:var(--t2);font-weight:300}
.s{font-size:12px;color:var(--t3);margin-top:8px}
.ev{display:flex;gap:16px;padding:5px 0;font-size:15px;line-height:1.5;color:var(--t1)}
.ev .h{width:74px;color:var(--t3);flex:none;font-variant-numeric:tabular-nums;font-weight:300}
.row{display:flex;gap:28px;flex-wrap:wrap;font-size:16px;color:var(--t1);font-weight:300}
.row small{color:var(--t3);font-size:12px;margin-left:4px}
.title{font-size:30px;font-weight:200;color:var(--t1);letter-spacing:-.5px;margin:34px 0 4px}
.sub{font-size:13px;color:var(--t3);margin-bottom:22px;font-weight:300}
.n{padding:14px 0;border-top:1px solid var(--line);font-size:15px;line-height:1.55;color:var(--t1);font-weight:300}
.n small{display:block;font-size:10px;letter-spacing:2px;text-transform:uppercase;color:var(--t3);margin-bottom:5px;font-weight:500}
.form{display:flex;flex-direction:column;gap:10px;margin-bottom:26px}
.form input,.form textarea{width:100%;background:#0a0a0a;border:1px solid #232323;border-radius:12px;color:var(--t1);
font:inherit;font-size:16px;padding:13px 14px;outline:none;-webkit-appearance:none;appearance:none}
.form input:focus,.form textarea:focus{border-color:#4a4a4a}
.form textarea{min-height:76px;resize:none}
.btn{background:transparent;border:1px solid #3a3a3a;border-radius:12px;color:var(--t1);font:inherit;font-size:14px;
letter-spacing:1.5px;text-transform:uppercase;padding:13px;cursor:pointer}
.btn:active{background:#151515}
.rem{padding:14px 0;border-top:1px solid var(--line)}
.rem .w{font-size:11px;letter-spacing:1.5px;text-transform:uppercase;color:var(--t3)}
.rem .ti{font-size:16px;color:var(--t1);margin:4px 0 2px}
.rem .no{font-size:14px;color:var(--t2);font-weight:300;white-space:pre-wrap}
.rem .ac{display:flex;gap:18px;margin-top:10px;font-size:12px;letter-spacing:1.2px;text-transform:uppercase}
.rem .ac a{color:var(--t3);cursor:pointer}
.rem.past .ti{color:var(--t3);text-decoration:line-through}
.nav{position:fixed;left:0;right:0;bottom:0;display:flex;justify-content:center;gap:26px;
padding:22px 0 calc(env(safe-area-inset-bottom,0px) + 16px);
background:linear-gradient(rgba(0,0,0,0),#000 55%);font-size:10.5px;letter-spacing:2px;text-transform:uppercase;color:var(--t4)}
.nav a{cursor:pointer;transition:color .3s}
.nav a.on{color:var(--t1)}
"""

JS = r"""
(function(){
  var deck=document.getElementById('deck');
  var navs=document.querySelectorAll('.nav a');
  function go(i,smooth){deck.scrollTo({left:i*deck.clientWidth,behavior:smooth?'smooth':'auto'});}
  function mark(){var i=Math.round(deck.scrollLeft/deck.clientWidth);
    navs.forEach(function(a,k){a.classList.toggle('on',k===i);});}
  navs.forEach(function(a){a.addEventListener('click',function(){go(+a.dataset.i,true);});});
  deck.addEventListener('scroll',mark,{passive:true});
  requestAnimationFrame(function(){go(1,false);mark();});
  window.addEventListener('load',function(){go(1,false);mark();});

  var h=new Date().getHours();
  document.getElementById('hello').textContent=
    h<5?'Dobrej nocy':h<12?'Dzień dobry':h<18?'Miłego dnia':'Dobry wieczór';

  var KEY='skeyo_reminders';
  function load(){try{return JSON.parse(localStorage.getItem(KEY)||'[]');}catch(e){return [];}}
  function save(a){try{localStorage.setItem(KEY,JSON.stringify(a));}catch(e){}}
  function fmt(s){return new Date(s).toLocaleString('pl-PL',{weekday:'short',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'});}
  function pad(n){return String(n).padStart(2,'0');}
  function utc(x){return x.getUTCFullYear()+pad(x.getUTCMonth()+1)+pad(x.getUTCDate())+'T'+pad(x.getUTCHours())+pad(x.getUTCMinutes())+'00Z';}
  function ics(s){return String(s||'').replace(/\\/g,'\\\\').replace(/;/g,'\\;').replace(/,/g,'\\,').replace(/\n/g,'\\n');}

  function toCalendar(r){
    var d=new Date(r.when),end=new Date(d.getTime()+30*60000);
    var lines=['BEGIN:VCALENDAR','VERSION:2.0','PRODID:-//skeyo.oss//PL','BEGIN:VEVENT',
      'UID:'+r.id+'@skeyo.oss','DTSTAMP:'+utc(new Date()),'DTSTART:'+utc(d),'DTEND:'+utc(end),
      'SUMMARY:'+ics(r.title),'DESCRIPTION:'+ics(r.note),
      'BEGIN:VALARM','ACTION:DISPLAY','DESCRIPTION:'+ics(r.title),'TRIGGER:-PT15M','END:VALARM',
      'BEGIN:VALARM','ACTION:DISPLAY','DESCRIPTION:'+ics(r.title),'TRIGGER:PT0M','END:VALARM',
      'END:VEVENT','END:VCALENDAR'];
    var a=document.createElement('a');
    a.href='data:text/calendar;charset=utf-8,'+encodeURIComponent(lines.join('\r\n'));
    a.download='przypomnienie.ics';
    document.body.appendChild(a);a.click();document.body.removeChild(a);
  }

  function el(tag,cls,txt){var e=document.createElement(tag);if(cls)e.className=cls;if(txt!==undefined)e.textContent=txt;return e;}

  function render(){
    var all=load().sort(function(x,y){return new Date(x.when)-new Date(y.when);});
    var box=document.getElementById('rlist');box.textContent='';
    if(!all.length){box.appendChild(el('div','t2','Brak przypomnień.'));}
    var now=Date.now();
    all.forEach(function(r){
      var d=el('div','rem'+(new Date(r.when).getTime()<now?' past':''));
      d.appendChild(el('div','w',fmt(r.when)));
      d.appendChild(el('div','ti',r.title));
      if(r.note)d.appendChild(el('div','no',r.note));
      var ac=el('div','ac');
      var c=el('a',null,'Do kalendarza');c.addEventListener('click',function(){toCalendar(r);});
      var x=el('a',null,'Usuń');x.addEventListener('click',function(){
        save(load().filter(function(q){return q.id!==r.id;}));render();});
      ac.appendChild(c);ac.appendChild(x);d.appendChild(ac);box.appendChild(d);
    });
    var tx=document.getElementById('todayx');tx.textContent='';
    var today=new Date().toDateString();
    all.filter(function(r){return new Date(r.when).toDateString()===today;}).forEach(function(r){
      var e=el('div','ev');
      e.appendChild(el('span','h',new Date(r.when).toLocaleTimeString('pl-PL',{hour:'2-digit',minute:'2-digit'})));
      e.appendChild(el('span',null,r.title));
      tx.appendChild(e);
    });
  }

  document.getElementById('add').addEventListener('click',function(){
    var t=document.getElementById('rt'),w=document.getElementById('rw'),n=document.getElementById('rn');
    if(!t.value.trim()||!w.value){alert('Wpisz tytuł i wybierz datę.');return;}
    var r={id:Date.now().toString(36),title:t.value.trim(),when:w.value,note:n.value.trim()};
    var a=load();a.push(r);save(a);
    t.value='';w.value='';n.value='';render();
    if(confirm('Dodać też do kalendarza iPhone\'a, żeby dostać powiadomienie?'))toCalendar(r);
  });
  render();
})();
"""

TEMPLATE = """<!doctype html><html lang="pl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#000000">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<title>skeyo.oss</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@200;300;400;500;600&display=swap" rel="stylesheet">
<style>__CSS__</style></head><body>
<div class="deck" id="deck">
<section class="panel"><div class="in">__NEWS__</div></section>
<section class="panel"><div class="in">__MAIN__</div></section>
<section class="panel"><div class="in">
<div class="brand"><b>skeyo.oss</b><span>przypomnienia</span></div>
<div class="title">Przypomnienia</div>
<div class="sub">Dodaj datę, treść i notatkę.</div>
<div class="form">
<input id="rt" type="text" placeholder="Tytuł" autocomplete="off">
<input id="rw" type="datetime-local">
<textarea id="rn" placeholder="Notatka (opcjonalnie)"></textarea>
<button class="btn" id="add" type="button">Dodaj</button>
</div>
<div id="rlist"></div>
</div></section>
</div>
<nav class="nav"><a data-i="0">Świat</a><a data-i="1" class="on">Dziś</a><a data-i="2">Przypomnienia</a></nav>
<script>__JS__</script>
</body></html>"""


def render(ai, crypto):
    day = DAYS[NOW.weekday()]
    date = f"{NOW.day} {MONTHS[NOW.month - 1]}"
    skipped = ai.get("poczta_pominieto", 0)

    crypto_html = ""
    if crypto:
        crypto_html = '<div class="row">' + "".join(
            f'<span>{k} {fmt_price(v["usd"])}<small>{fmt_change(v["zmiana"])}</small></span>'
            for k, v in crypto.items()
        ) + "</div>"

    main = (
        f'<div class="brand"><b>skeyo.oss</b><span>akt. {NOW:%H:%M}</span></div>'
        f'<div class="hello" id="hello"></div>'
        f'<div><div class="day">{esc(day)}</div><div class="date">{esc(date)}</div></div>'
        f'<div class="sec"><div class="lab">Pogoda · {esc(PLACE)}</div>'
        f'<div class="temp">{esc(ai.get("temperatura", ""))}</div>'
        f'<div class="t2">{esc(ai.get("pogoda", "Brak danych"))}</div></div>'
        f'<div class="sec"><div class="lab">Dziś</div>'
        f'{event_lines(ai.get("dzis", ["Brak danych"]))}<div id="todayx"></div></div>'
        f'<div class="sec"><div class="lab">Poczta</div>{plain_lines(ai.get("poczta", []))}'
        + (f'<div class="s">Pominięto {esc(skipped)} mniej ważnych</div>' if skipped else "")
        + "</div>"
        f'<div class="sec"><div class="lab">Rynek</div>{crypto_html}</div>'
    )

    news = (
        '<div class="brand"><b>skeyo.oss</b><span>świat</span></div>'
        '<div class="title">Świat</div>'
        f'<div class="sub">Najważniejsze wiadomości · {esc(date)}</div>'
        f'{news_items(ai.get("wiadomosci", []))}'
    )

    return (TEMPLATE.replace("__CSS__", CSS).replace("__JS__", JS)
            .replace("__MAIN__", main).replace("__NEWS__", news))


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
        ai = {"pogoda": "Nie udało się przygotować podsumowania.", "dzis": [], "poczta": [], "wiadomosci": []}

    os.makedirs("site", exist_ok=True)
    with open("site/index.html", "w", encoding="utf-8") as f:
        f.write(render(ai, crypto))
    print("Gotowe: site/index.html")


if __name__ == "__main__":
    main()
