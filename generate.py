#!/usr/bin/env python3
"""Morning Briefing: Recherche, Skript, Vertonung und Feed in einem Durchlauf.
 
Läuft in GitHub Actions. Braucht die Umgebungsvariable GEMINI_API_KEY.
Schritte:
  1. Recherche mit Gemini + Google Suche
  2. Dialogskript (Mia und Jonas) schreiben
  3. Vertonung mit Gemini TTS (zwei Stimmen), Zusammenfügen zu MP3
  4. Shownotes, episodes.json und feed.xml im Ordner site/ aktualisieren
Die MP3 landet in out/ und wird vom Workflow als GitHub Release hochgeladen.
"""
import base64, html, json, os, re, subprocess, sys, time
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from zoneinfo import ZoneInfo
 
import requests
 
ROOT = Path(__file__).resolve().parent
SITE = ROOT / "site"
OUT = ROOT / "out"
API = "https://generativelanguage.googleapis.com/v1beta"
KEY = os.environ["GEMINI_API_KEY"]
REPO = os.environ.get("GITHUB_REPOSITORY", "Lu1s-15/morning-briefing")
OWNER, NAME = REPO.split("/")
PAGES = f"https://{OWNER.lower()}.github.io/{NAME}/"
TZ = ZoneInfo("Europe/Zurich")
NOW = datetime.now(TZ)
DATE = NOW.strftime("%Y-%m-%d")
WOCHENTAGE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
MONATE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
          "November", "Dezember"]
DATUM_LANG = f"{WOCHENTAGE[NOW.weekday()]}, {NOW.day}. {MONATE[NOW.month - 1]} {NOW.year}"
 
TTS_MODELS = [m for m in os.environ.get("TTS_MODELS", "").split(",") if m] or [
    "gemini-3.8-flash-tts", "gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts"]
VOICES = {"Mia": os.environ.get("VOICE_MIA", "Kore"), "Jonas": os.environ.get("VOICE_JONAS", "Charon")}
 
 
def log(msg):
    print(f"[{datetime.now(TZ):%H:%M:%S}] {msg}", flush=True)
 
 
class QuotaError(RuntimeError):
    pass
 
 
def quota_info(text):
    """Holt die relevanten Kontingent Angaben aus einer 429 Antwort."""
    try:
        details = json.loads(text)["error"].get("details", [])
    except Exception:
        return text[:300]
    out = []
    for d in details:
        for v in d.get("violations", []):
            out.append(f"{v.get('quotaMetric', '')} / {v.get('quotaId', '')} / Limit {v.get('quotaValue', '?')}")
        if d.get("retryDelay"):
            out.append(f"retry in {d['retryDelay']}")
    return "; ".join(out) or text[:300]
 
 
def post(model, body, timeout=300):
    """generateContent. Wartet nur bei einem klar ausgewiesenen Minutenlimit, sonst sofort Fehler."""
    last = None
    for attempt in range(3):
        try:
            r = requests.post(f"{API}/models/{model}:generateContent", json=body, timeout=timeout,
                              headers={"x-goog-api-key": KEY, "Content-Type": "application/json"})
        except requests.RequestException as e:
            last = f"{model}: Netzwerkfehler {e}"
            time.sleep(15)
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429:
            info = quota_info(r.text)
            last = f"{model}: HTTP 429 ({info[:200]})"
            if "PerMinute" in info and "Limit 0" not in info and attempt < 2:
                log(f"{last} ... Minutenlimit, warte 65s")
                time.sleep(65)
                continue
            raise QuotaError(last)
        last = f"{model}: HTTP {r.status_code} {r.text[:400]}"
        if r.status_code in (400, 403, 404):
            break
        time.sleep(20)
    raise RuntimeError(last)
 
 
def with_fallback(models, fn):
    """Probiert eine Liste von Modellen der Reihe nach durch."""
    errors = []
    for m in models:
        try:
            return fn(m)
        except Exception as e:
            log(f"{m} geht nicht: {str(e)[:300]}")
            errors.append(str(e)[:300])
    raise RuntimeError("Alle Modelle fehlgeschlagen:\n" + "\n".join(errors))
 
 
def text_of(resp):
    parts = resp["candidates"][0]["content"].get("parts", [])
    return "".join(p.get("text", "") for p in parts).strip()
 
 
def pick_text_models():
    """Liste der Flash Textmodelle, neueste zuerst, Flash-Lite als Reserve am Schluss."""
    if os.environ.get("TEXT_MODEL"):
        return [m for m in os.environ["TEXT_MODEL"].split(",") if m]
    r = requests.get(f"{API}/models?pageSize=1000", headers={"x-goog-api-key": KEY}, timeout=60)
    r.raise_for_status()
    bad = ("tts", "image", "live", "audio", "embedding", "vision", "thinking", "exp", "learnlm", "robotics")
    cands = []
    for m in r.json().get("models", []):
        name = m["name"].split("/", 1)[1]
        if "generateContent" not in m.get("supportedGenerationMethods", []):
            continue
        if "flash" not in name or any(b in name for b in bad):
            continue
        ver = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
        if not ver or float(ver.group(1)) < 2.5:
            continue
        cands.append(("lite" not in name, float(ver.group(1)), "preview" not in name, -len(name), name))
    if not cands:
        raise RuntimeError("Kein passendes Gemini Flash Textmodell gefunden")
    cands.sort(reverse=True)
    names = [c[4] for c in cands][:8]
    log("Modell Reihenfolge: " + ", ".join(names))
    return names
 
 
def load_instructions():
    lines = (ROOT / "anweisungen.md").read_text(encoding="utf-8").splitlines()
    return "\n".join(l for l in lines if not l.lstrip().startswith("# ") and l.strip() != "#").strip()
 
 
def recent_topics(eps):
    recent = sorted(eps, key=lambda e: e["date"], reverse=True)[:5]
    return "\n".join(f"- {e['date']}: {e.get('topics', '')}" for e in recent) or "- (noch keine)"
 
 
def research(model, instr, recent):
    log(f"Recherche mit {model} + Google Suche")
    prompt = f"""Heute ist {DATUM_LANG}, Zeitzone Europe/Zurich.
Du bist Rechercheur für ein persönliches Morgen News Briefing. Nutze die Google Suche gründlich (viele Suchen,
Schweizer und internationale Quellen, auch englischsprachige) und sammle die wichtigsten und interessantesten
Meldungen der letzten 24 bis 36 Stunden gemäss diesen Anweisungen:
 
{instr}
 
Bereits behandelte Themen der letzten Tage (nicht wiederholen, ausser es gibt eine echte neue Entwicklung):
{recent}
 
Liefere ein Faktendossier auf Deutsch, KEIN Skript:
- 6 bis 9 Geschichten, nach Wichtigkeit sortiert, jeweils mit: Was ist passiert, konkrete Zahlen und Namen,
  Hintergrund, warum es wichtig ist, und die Quelle (Medium und URL).
- Dazu Vorschläge für "Kurz erklärt", "Zahl des Tages" und "Wildcard", ebenfalls mit Quelle.
- Nur Fakten, die du in den Suchergebnissen gefunden hast. Nichts erfinden. Wenn Zahlen widersprüchlich sind,
  sag das. Datum jeder Meldung prüfen: nichts Altes als neu verkaufen."""
    resp = post(model, {"contents": [{"parts": [{"text": prompt}]}], "tools": [{"google_search": {}}],
                        "generationConfig": {"temperature": 0.4}})
    dossier = text_of(resp)
    sources = []
    meta = resp["candidates"][0].get("groundingMetadata", {})
    for ch in meta.get("groundingChunks", []):
        w = ch.get("web", {})
        if w.get("uri"):
            sources.append((w.get("title") or "Quelle", w["uri"]))
    if len(dossier) < 1500:
        raise RuntimeError("Dossier zu kurz, Recherche fehlgeschlagen:\n" + dossier)
    return dossier, sources
 
 
FEEDS = [
    ("SRF News", "https://www.srf.ch/news/bnf/rss/1646"),
    ("SRF Schweiz", "https://www.srf.ch/news/bnf/rss/1890"),
    ("SRF International", "https://www.srf.ch/news/bnf/rss/1922"),
    ("SRF Wirtschaft", "https://www.srf.ch/news/bnf/rss/1926"),
    ("NZZ", "https://www.nzz.ch/recent.rss"),
    ("NZZ Wirtschaft", "https://www.nzz.ch/wirtschaft.rss"),
    ("Handelszeitung", "https://www.handelszeitung.ch/rss.xml"),
    ("tagesschau", "https://www.tagesschau.de/index~rss2.xml"),
    ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("Guardian World", "https://www.theguardian.com/world/rss"),
    ("Guardian Business", "https://www.theguardian.com/business/rss"),
    ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("TechCrunch", "https://techcrunch.com/feed/"),
    ("The Verge", "https://www.theverge.com/rss/index.xml"),
    ("persoenlich.com", "https://www.persoenlich.com/rss"),
    ("Marketing Week", "https://www.marketingweek.com/feed/"),
    ("SportBusiness", "https://www.sportbusiness.com/feed/"),
    ("Carbon Brief", "https://www.carbonbrief.org/feed"),
    ("STAT News", "https://www.statnews.com/feed/"),
    ("ScienceDaily", "https://www.sciencedaily.com/rss/top/science.xml"),
    ("Space.com", "https://www.space.com/feeds/all"),
]
 
 
def fetch_feeds(hours=36, per_feed=15):
    """Holt aktuelle Meldungen aus festen RSS Feeds (ohne Google Suche)."""
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
    items, ok = [], 0
    for src, url in FEEDS:
        try:
            r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 (MorningBriefing RSS reader)"})
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception as e:
            log(f"Feed {src} nicht lesbar: {str(e)[:80]}")
            continue
        ok += 1
        n = 0
        for it in root.iter():
            tag = it.tag.split("}")[-1]
            if tag not in ("item", "entry"):
                continue
            get = lambda *names: next((c.text or c.get("href") or "" for c in it if c.tag.split("}")[-1] in names), "")
            title, link = get("title").strip(), get("link").strip()
            desc = re.sub(r"<[^>]+>", " ", get("description", "summary", "content")).strip()
            desc = re.sub(r"\s+", " ", html.unescape(desc))[:350]
            when = get("pubDate", "updated", "published", "date")
            ts = None
            try:
                ts = parsedate_to_datetime(when).timestamp()
            except Exception:
                try:
                    ts = datetime.fromisoformat(when.replace("Z", "+00:00")).timestamp()
                except Exception:
                    pass
            if not title or (ts and ts < cutoff):
                continue
            items.append(f"[{src}] {title} | {desc} | {link}")
            n += 1
            if n >= per_feed:
                break
    log(f"RSS: {ok}/{len(FEEDS)} Feeds gelesen, {len(items)} Meldungen")
    if len(items) < 20:
        raise RuntimeError("Zu wenige RSS Meldungen")
    return items
 
 
def research_rss(model, instr, recent, items):
    log(f"Recherche aus RSS Feeds mit {model}")
    prompt = f"""Heute ist {DATUM_LANG}, Zeitzone Europe/Zurich.
Du bist Rechercheur für ein persönliches Morgen News Briefing. Unten stehen aktuelle Meldungen aus RSS Feeds
(Format: [Quelle] Titel | Anriss | Link). Wähle gemäss diesen Anweisungen die wichtigsten und interessantesten aus:
 
{instr}
 
Bereits behandelte Themen der letzten Tage (nicht wiederholen, ausser es gibt eine echte neue Entwicklung):
{recent}
 
Liefere ein Faktendossier auf Deutsch, KEIN Skript:
- 6 bis 9 Geschichten, nach Wichtigkeit sortiert, jeweils mit: Was ist passiert, konkrete Zahlen und Namen,
  warum es wichtig ist, Quelle und Link.
- Dazu Vorschläge für "Kurz erklärt", "Zahl des Tages" und "Wildcard".
- Verwende NUR Fakten aus den Meldungen unten. Allgemeines Hintergrundwissen zur Einordnung ist erlaubt, aber
  als solches erkennbar und ohne erfundene Zahlen. Wenn ein Anriss zu dünn ist, lieber weglassen.
 
MELDUNGEN:
""" + "\n".join(items[:250])
    resp = post(model, {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.4}})
    dossier = text_of(resp)
    if len(dossier) < 1500:
        raise RuntimeError("Dossier zu kurz:\n" + dossier[:500])
    return dossier, []
 
 
def working_models(models):
    """Testet kurz, welche Modelle überhaupt antworten (ohne Google Suche)."""
    good = []
    for m in models:
        try:
            post(m, {"contents": [{"parts": [{"text": "Antworte nur mit: OK"}]}]}, timeout=60)
            log(f"Test {m}: OK")
            good.append(m)
            if len(good) >= 3:
                break
        except Exception as e:
            log(f"Test {m}: {str(e)[:200]}")
    if not good:
        raise RuntimeError("Kein Gemini Modell antwortet, auch ohne Google Suche. Der Schlüssel bzw. das Google "
                           "Projekt hat kein nutzbares Kontingent (Abrechnung oder neues Projekt nötig).")
    return good
 
 
def write_script(model, instr, dossier):
    log(f"Skript mit {model}")
    prompt = f"""Schreibe aus dem folgenden Faktendossier das Skript für die heutige Folge des "Morning Briefing"
({DATUM_LANG}). Halte dich an diese Anweisungen:
 
{instr}
 
Formatregeln (sehr wichtig, das Skript wird automatisch vertont):
- Genau 6 Teile. Jeder Teil beginnt mit einer eigenen Zeile "### Teil 1", "### Teil 2" usw.
- Jede Dialogzeile beginnt mit "Mia: " oder "Jonas: ". Keine anderen Sprecher, keine Regieanweisungen,
  keine Klammern, keine Markdown Formatierung, keine Aufzählungszeichen, keine URLs.
- Jeder Teil hat etwa 330 bis 380 Wörter.
- Teil 1 beginnt mit Begrüssung und Datum und einem kurzen Teaser, Teil 6 endet mit kurzer Zusammenfassung
  und Verabschiedung ("Bis morgen").
- Die Rubriken "Kurz erklärt", "Zahl des Tages" und "Wildcard" kommen klar angekündigt vor.
- Abkürzungen ausschreiben oder so schreiben, wie man sie spricht. Prozent, Franken, Dollar als Wort.
- Verwende NUR Fakten aus dem Dossier. Erfinde nichts dazu, keine Zitate, die nicht im Dossier stehen.
 
Nach dem Skript, getrennt durch eine Zeile "=== SHOWNOTES ===", schreibe:
- Eine Zeile "Themen: ..." mit den behandelten Geschichten, kommagetrennt, kurz.
- Eine Zeile "Titel: ..." mit einem knackigen Folgentitel (max. 70 Zeichen).
- Danach die Quellen aus dem Dossier als Liste "Medium: URL".
 
FAKTENDOSSIER:
{dossier}"""
    resp = post(model, {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.8}})
    full = text_of(resp)
    if "=== SHOWNOTES ===" not in full:
        raise RuntimeError("Skript ohne Shownotes Trenner:\n" + full[:1000])
    script, notes = full.split("=== SHOWNOTES ===", 1)
    return script.strip(), notes.strip()
 
 
def split_parts(script):
    parts = [p.split("\n", 1)[1].strip() for p in re.split(r"^###\s*", script, flags=re.M)
             if p.strip() and "\n" in p and p.strip().lower().startswith("teil")]
    clean = []
    for p in parts:
        lines = [l.strip() for l in p.splitlines() if re.match(r"^(Mia|Jonas):\s*\S", l.strip())]
        if lines:
            clean.append("\n".join(lines))
    if len(clean) < 3:
        raise RuntimeError("Skript konnte nicht in Teile zerlegt werden")
    return clean
 
 
STYLE = ("Das ist ein deutschsprachiger News und Wirtschaftspodcast mit zwei Hosts, Mia und Jonas. "
         "Sprecht Hochdeutsch, natürlich, locker und mit Energie, wie zwei Freunde, die sich spannende Geschichten "
         "erzählen. Zahlen klar und deutlich. Lies nur den Dialog vor:\n\n")
 
 
def tts(text):
    body = {"contents": [{"parts": [{"text": STYLE + text}]}],
            "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {"multiSpeakerVoiceConfig": {
                "speakerVoiceConfigs": [{"speaker": s, "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": v}}}
                                        for s, v in VOICES.items()]}}}}
    errors = []
    for model in TTS_MODELS:
        try:
            resp = post(model, body)
            parts = resp["candidates"][0]["content"]["parts"]
            pcm = b"".join(base64.b64decode(p["inlineData"]["data"]) for p in parts if "inlineData" in p)
            if pcm:
                return pcm, model
            errors.append(f"{model}: keine Audiodaten")
        except Exception as e:  # nächstes Modell probieren
            log(f"{model} geht nicht: {str(e)[:300]}")
            errors.append(str(e)[:300])
    raise RuntimeError("TTS fehlgeschlagen: " + " | ".join(errors))
 
 
def render(parts):
    OUT.mkdir(exist_ok=True)
    wavs = []
    for i, p in enumerate(parts):
        if i:
            time.sleep(20)  # Rate Limit schonen
        pcm, model = tts(p)
        wav = OUT / f"part{i}.wav"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "s16le", "-ar", "24000", "-ac", "1",
                        "-i", "pipe:0", str(wav)], input=pcm, check=True)
        wavs.append(wav)
        log(f"Teil {i + 1}/{len(parts)} vertont ({model}, {len(pcm) / 48000:.0f} s)")
    lst = OUT / "list.txt"
    lst.write_text("".join(f"file '{w.name}'\n" for w in wavs))
    mp3 = OUT / f"{DATE}.mp3"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-af", "loudnorm=I=-16:TP=-1.5", "-ac", "1", "-b:a", "64k",
                    "-metadata", f"title=Morning Briefing {DATE}", str(mp3)], check=True)
    for w in wavs:
        w.unlink()
    lst.unlink()
    return mp3
 
 
def duration(mp3):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(mp3)],
                         capture_output=True, text=True, check=True).stdout.strip()
    return int(float(out))
 
 
def build_feed(eps):
    items = []
    for e in sorted(eps, key=lambda x: x["date"], reverse=True):
        items.append(f"""    <item>
      <title>{html.escape(e['title'])}</title>
      <description><![CDATA[{e['notes_html']}]]></description>
      <pubDate>{e['pub']}</pubDate>
      <guid isPermaLink="false">briefing-{e['date']}</guid>
      <enclosure url="{html.escape(e['url'])}" length="{e['bytes']}" type="audio/mpeg"/>
      <itunes:duration>{e['dur']}</itunes:duration>
    </item>""")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>Morning Briefing</title>
    <link>{PAGES}</link>
    <language>de</language>
    <description>Persönliches Morning Briefing: Politik, Wirtschaft, Finanzen und mehr. Automatisch erstellt mit KI, Angaben ohne Gewähr.</description>
    <itunes:author>Morning Briefing</itunes:author>
    <itunes:explicit>false</itunes:explicit>
    <itunes:block>Yes</itunes:block>
    <itunes:image href="{PAGES}cover.jpg"/>
{chr(10).join(items)}
  </channel>
</rss>
"""
 
 
def build_index(eps):
    rows = "\n".join(
        f'<li><b>{html.escape(e["title"])}</b> ({e["date"]})<br><audio controls preload="none" src="{html.escape(e["url"])}"></audio></li>'
        for e in sorted(eps, key=lambda x: x["date"], reverse=True))
    return f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Morning Briefing</title>
<style>body{{font-family:-apple-system,system-ui,sans-serif;max-width:640px;margin:2rem auto;padding:0 16px}}
li{{margin:1.2rem 0}}audio{{width:100%;margin-top:.4rem}}code{{word-break:break-all}}</style></head><body>
<h1>Morning Briefing</h1><p>Feed zum Abonnieren: <code>{PAGES}feed.xml</code></p><ul>{rows}</ul></body></html>
"""
 
 
def ensure_cover():
    cover = SITE / "cover.jpg"
    if cover.exists():
        return
    try:
        from PIL import Image, ImageDraw, ImageFont
        im = Image.new("RGB", (1400, 1400), (18, 24, 38))
        d = ImageDraw.Draw(im)
        d.ellipse((850, -250, 1650, 550), fill=(242, 166, 59))
        fdir = "/usr/share/fonts/truetype/dejavu/"
        try:
            big, small = ImageFont.truetype(fdir + "DejaVuSans-Bold.ttf", 150), ImageFont.truetype(fdir + "DejaVuSans.ttf", 60)
        except OSError:
            big = small = ImageFont.load_default()
        d.text((110, 820), "Morning", font=big, fill="white")
        d.text((110, 990), "Briefing", font=big, fill=(242, 166, 59))
        d.text((115, 1190), "Politik · Wirtschaft · Finanzen", font=small, fill=(190, 200, 215))
        im.save(cover, quality=90)
    except Exception as e:
        log(f"Cover konnte nicht erstellt werden: {e}")
 
 
def main():
    SITE.mkdir(exist_ok=True)
    ensure_cover()
    idx = SITE / "episodes.json"
    eps = json.loads(idx.read_text()) if idx.exists() else []
    if any(e["date"] == DATE for e in eps) and os.environ.get("FORCE") != "1":
        log(f"Folge für {DATE} existiert schon, nichts zu tun.")
        return
    eps = [e for e in eps if e["date"] != DATE]
 
    instr = load_instructions()
    models = working_models(pick_text_models())
    recent = recent_topics(eps)
    try:
        dossier, grounding = with_fallback(models[:2], lambda m: research(m, instr, recent))
    except Exception as e:
        log(f"Google Suche nicht nutzbar, weiche auf RSS Feeds aus ({str(e)[:150]})")
        items = fetch_feeds()
        dossier, grounding = with_fallback(models, lambda m: research_rss(m, instr, recent, items))
    script, notes = with_fallback(models, lambda m: write_script(m, instr, dossier))
    OUT.mkdir(exist_ok=True)
    (OUT / f"{DATE}-skript.txt").write_text(script, encoding="utf-8")
    (OUT / f"{DATE}-dossier.txt").write_text(dossier, encoding="utf-8")
 
    title_m = re.search(r"^Titel:\s*(.+)$", notes, re.M)
    topics_m = re.search(r"^Themen:\s*(.+)$", notes, re.M)
    title = f"{NOW.day}. {MONATE[NOW.month - 1]}: " + (title_m.group(1).strip() if title_m else "Morning Briefing")
    topics = topics_m.group(1).strip() if topics_m else ""
    notes_body = re.sub(r"^Titel:.*$\n?", "", notes, flags=re.M).strip()
    if grounding:
        notes_body += "\n\nSuchquellen (Google):\n" + "\n".join(f"{t}: {u}" for t, u in grounding[:25])
    notes_body += "\n\nAutomatisch mit KI erstellt. Angaben ohne Gewähr, bitte bei Bedarf in den Quellen prüfen."
 
    mp3 = render(split_parts(script))
    url = f"https://github.com/{REPO}/releases/download/folge-{DATE}/{mp3.name}"
    notes_html = "<br/>".join(re.sub(r"(https?://\S+)", r'<a href="\1">\1</a>', html.escape(l, quote=False))
                              for l in notes_body.splitlines())
    eps.append({"date": DATE, "title": title, "topics": topics, "url": url, "bytes": mp3.stat().st_size,
                "dur": duration(mp3), "pub": format_datetime(datetime.now(timezone.utc)),
                "notes_html": notes_html})
    eps = sorted(eps, key=lambda e: e["date"], reverse=True)[:30]  # Feed hält 30 Folgen
    idx.write_text(json.dumps(eps, ensure_ascii=False, indent=1), encoding="utf-8")
    (SITE / "feed.xml").write_text(build_feed(eps), encoding="utf-8")
    (SITE / "index.html").write_text(build_index(eps), encoding="utf-8")
    (OUT / "release-notes.txt").write_text(f"{title}\n\n{notes_body}", encoding="utf-8")
    (OUT / "title.txt").write_text(title, encoding="utf-8")
    log(f"Fertig: {title} ({duration(mp3) // 60} min)")
 
 
if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FEHLER: {e}")
        sys.exit(1)
 
