from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from dotenv import load_dotenv
import os
import re
import json
import requests
import psycopg2
import psycopg2.extras
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timezone

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "bitte-spaeter-sicher-ersetzen")
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "None"
app.config["SESSION_COOKIE_PARTITIONED"] = True
app.config["SESSION_COOKIE_NAME"] = "chatbot_session_v3"

# -----------------------------
# API / externes LLM
# -----------------------------
LLM_API_KEY = os.environ.get("LLM_API_KEY", "").strip()
LLM_MODEL = os.environ.get("LLM_MODEL", "GPT OSS 120B").strip()
LLM_API_URL = os.environ.get(
    "LLM_API_URL",
    "https://ki-chat.uni-mainz.de/api/chat/completions"
).strip()

SEAFILE_BASE_URL = os.environ.get("SEAFILE_BASE_URL", "").strip().rstrip("/")
SEAFILE_TOKEN = os.environ.get("SEAFILE_TOKEN", "").strip()
SEAFILE_REPO_ID = os.environ.get("SEAFILE_REPO_ID", "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

# Gesprächsdauer: 7 Minuten 30 Sekunden.
# Nach Ablauf wird nicht automatisch beendet.
# Erst nach der nächsten Nutzer-Nachricht sendet Lumi die Abschlussnachricht.
CONVERSATION_DURATION_SECONDS = int(
    os.environ.get(
        "CONVERSATION_DURATION_SECONDS",
        str(int(float(os.environ.get("CONVERSATION_DURATION_MINUTES", "1.5")) * 60))
    )
)

# Pause nach der Abschlussnachricht, bevor der nächste Studientag startet.
DAY_SWITCH_PAUSE_SECONDS = int(
    os.environ.get(
        "DAY_SWITCH_PAUSE_SECONDS",
        str(int(float(os.environ.get("DAY_SWITCH_PAUSE_MINUTES", "1")) * 60))
    )
)

MAX_STUDY_DAY = 5


# -----------------------------
# Login, Datenbank und Seafile-Speicherung
# -----------------------------
def get_db_connection():
    if not DATABASE_URL:
        raise Exception("DATABASE_URL ist nicht gesetzt.")
    return psycopg2.connect(DATABASE_URL)


def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    cur.close()
    conn.close()


def create_user(username, password):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO users (username, password_hash) VALUES (%s, %s)",
        (username, generate_password_hash(password))
    )
    conn.commit()
    cur.close()
    conn.close()


def get_user_by_username(username):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, username, password_hash FROM users WHERE username = %s", (username,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    return user


try:
    init_db()
    print("Datenbank initialisiert.")
except Exception as e:
    print("Datenbank-Initialisierung fehlgeschlagen:", repr(e))


def require_login():
    return "username" in session


def get_current_username():
    return session.get("username", "unknown")


def make_safe_filename(value):
    value = str(value or "unknown").strip()
    return re.sub(r"[^a-zA-Z0-9_-]", "_", value)


def get_chat_filename_for_day(day):
    username = make_safe_filename(get_current_username())
    return f"{username}_day{int(day)}.json"


def get_memory_filename():
    username = make_safe_filename(get_current_username())
    return f"{username}_memory.json"


def get_file_path(filename):
    return f"/{filename}"


def seafile_headers():
    return {
        "Authorization": f"Token {SEAFILE_TOKEN}",
        "Accept": "application/json"
    }


def ensure_seafile_config():
    missing = []
    if not SEAFILE_BASE_URL:
        missing.append("SEAFILE_BASE_URL")
    if not SEAFILE_TOKEN:
        missing.append("SEAFILE_TOKEN")
    if not SEAFILE_REPO_ID:
        missing.append("SEAFILE_REPO_ID")
    if missing:
        raise Exception("Fehlende Seafile-Variable(n): " + ", ".join(missing))


def get_upload_link():
    ensure_seafile_config()
    url = f"{SEAFILE_BASE_URL}/api2/repos/{SEAFILE_REPO_ID}/upload-link/"
    response = requests.get(url, headers=seafile_headers(), timeout=30)
    if response.status_code != 200:
        raise Exception(f"Upload-Link fehlgeschlagen: {response.status_code} {response.text}")
    return response.text.strip('"')


def get_update_link():
    ensure_seafile_config()
    url = f"{SEAFILE_BASE_URL}/api2/repos/{SEAFILE_REPO_ID}/update-link/"
    response = requests.get(url, headers=seafile_headers(), timeout=30)
    if response.status_code != 200:
        raise Exception(f"Update-Link fehlgeschlagen: {response.status_code} {response.text}")
    return response.text.strip('"')


def get_download_link_for_path(path):
    ensure_seafile_config()
    url = f"{SEAFILE_BASE_URL}/api2/repos/{SEAFILE_REPO_ID}/file/"
    response = requests.get(url, headers=seafile_headers(), params={"p": path}, timeout=30)
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise Exception(f"Download-Link fehlgeschlagen: {response.status_code} {response.text}")
    return response.text.strip('"')


def load_json_file_from_seafile(filename, default_value):
    try:
        download_link = get_download_link_for_path(get_file_path(filename))
        if not download_link:
            return default_value
        response = requests.get(download_link, timeout=30)
        if response.status_code != 200:
            return default_value
        return response.json()
    except Exception as e:
        print("Seafile-Laden fehlgeschlagen:", repr(e))
        return default_value


def upload_new_json_file_to_seafile(filename, payload):
    upload_link = get_upload_link()
    file_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    response = requests.post(
        upload_link,
        headers={"Authorization": f"Token {SEAFILE_TOKEN}"},
        files={"file": (filename, file_bytes, "application/json")},
        data={"parent_dir": "/", "replace": "1"},
        timeout=60
    )
    if response.status_code != 200:
        raise Exception(f"Upload fehlgeschlagen: {response.status_code} {response.text}")


def update_json_file_in_seafile(filename, payload):
    update_link = get_update_link()
    file_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    response = requests.post(
        update_link,
        headers={"Authorization": f"Token {SEAFILE_TOKEN}"},
        files={"file": (filename, file_bytes, "application/json")},
        data={"target_file": get_file_path(filename)},
        timeout=60
    )
    if response.status_code != 200:
        raise Exception(f"Update fehlgeschlagen: {response.status_code} {response.text}")


def save_json_file_to_seafile(filename, payload):
    existing = load_json_file_from_seafile(filename, None)
    if existing is None:
        upload_new_json_file_to_seafile(filename, payload)
    else:
        update_json_file_in_seafile(filename, payload)


def load_chat_history_from_seafile(day):
    data = load_json_file_from_seafile(get_chat_filename_for_day(day), [])
    return clean_history(data) if isinstance(data, list) else []


def save_chat_history_to_seafile(chat_history, day):
    save_json_file_to_seafile(get_chat_filename_for_day(day), clean_history(chat_history))


def load_participant_memory():
    data = load_json_file_from_seafile(get_memory_filename(), {})
    return data if isinstance(data, dict) else {}


def save_participant_memory(memory):
    memory["updated_at"] = utc_now_iso()
    save_json_file_to_seafile(get_memory_filename(), memory)


# -----------------------------
# Zeit- und Chat-Hilfsfunktionen
# -----------------------------
def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_iso_datetime(value):
    if not value:
        return None
    try:
        if isinstance(value, str) and value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def clean_history(chat_history):
    """Nimmt nur die Felder an, die der Server wirklich braucht."""
    if not isinstance(chat_history, list):
        return []

    cleaned = []
    for msg in chat_history:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue

        item = {
            "role": role,
            "content": content,
            "study_day": int(msg.get("study_day", 1) or 1),
        }
        for key in ("timestamp", "chat_started_at", "conversation_closed_at", "is_closing_message"):
            if key in msg:
                item[key] = msg[key]
        cleaned.append(item)

    return cleaned


def get_day_history(chat_history, study_day):
    return [
        msg for msg in clean_history(chat_history)
        if int(msg.get("study_day", 1) or 1) == int(study_day)
    ]


def get_chat_started_at(chat_history):
    for msg in chat_history:
        started_at = msg.get("chat_started_at") or msg.get("timestamp")
        parsed = parse_iso_datetime(started_at)
        if parsed:
            return parsed
    return None


def get_chat_elapsed_seconds(chat_history):
    started_at = get_chat_started_at(chat_history)
    if not started_at:
        return 0
    return max(0, int((datetime.now(timezone.utc) - started_at).total_seconds()))


def get_chat_closed_at(chat_history):
    for msg in reversed(chat_history):
        closed_at = msg.get("conversation_closed_at")
        parsed = parse_iso_datetime(closed_at)
        if parsed:
            return parsed
    return None


def chat_is_closed(chat_history):
    return get_chat_closed_at(chat_history) is not None


def chat_time_limit_reached(chat_history):
    return get_chat_elapsed_seconds(chat_history) >= CONVERSATION_DURATION_SECONDS


def next_day_is_unlocked(chat_history):
    closed_at = get_chat_closed_at(chat_history)
    if not closed_at:
        return False
    elapsed_after_closing = (datetime.now(timezone.utc) - closed_at).total_seconds()
    return elapsed_after_closing >= DAY_SWITCH_PAUSE_SECONDS


def get_active_study_day(chat_history=None):
    # Mit Login/Seafile ist Seafile die Quelle der Wahrheit.
    for day in range(1, MAX_STUDY_DAY + 1):
        day_history = load_chat_history_from_seafile(day)
        if not day_history:
            return day
        if not next_day_is_unlocked(day_history):
            return day
    return MAX_STUDY_DAY


def extract_preferred_name(text):
    if not text:
        return None

    patterns = [
        r"\b(?:ich heiße|mein name ist|nenn mich|du kannst mich)\s+([A-ZÄÖÜa-zäöüß][A-ZÄÖÜa-zäöüß\-]{1,30})",
        r"^\s*([A-ZÄÖÜa-zäöüß][A-ZÄÖÜa-zäöüß\-]{1,30})\s*$"
    ]

    for pattern in patterns:
        match = re.search(pattern, text.strip(), flags=re.IGNORECASE)
        if match:
            name = match.group(1).strip(" .,!?:;\n\t")
            if 2 <= len(name) <= 30:
                return name

    return None


def get_preferred_name_from_history(chat_history):
    for msg in clean_history(chat_history):
        if msg.get("role") == "user":
            name = extract_preferred_name(msg.get("content", ""))
            if name:
                return name
    return None


def update_participant_memory_from_message(user_message):
    memory = load_participant_memory()
    if not memory.get("preferred_name"):
        preferred_name = extract_preferred_name(user_message)
        if preferred_name:
            memory["preferred_name"] = preferred_name
            save_participant_memory(memory)


def get_previous_days_context(active_day, chat_history=None):
    context_parts = []
    memory = load_participant_memory()
    name = memory.get("preferred_name")

    if name:
        context_parts.append(
            f"Die teilnehmende Person hat sich Dir als {name} vorgestellt. "
            "Sprich sie, wenn passend, mit diesem Namen an."
        )

    for day in range(1, int(active_day)):
        history = load_chat_history_from_seafile(day)
        if not history:
            continue

        snippets = []
        for msg in history[-8:]:
            if msg.get("content"):
                role = "Teilnehmende Person" if msg.get("role") == "user" else "Lumi"
                snippets.append(f"{role}: {msg['content']}")

        if snippets:
            context_parts.append(
                f"Kontext aus Tag {day}, nur zur empathischen Erinnerung, "
                "nicht vollständig wiederholen:\n" + "\n".join(snippets)
            )

    return "\n\n".join(context_parts)


COMMON_HIGH_SELF_DISCLOSURE_PROMPT = """
Dein Name ist Lumi und du wurdest als Chat-Bot für Gesundheitsempfehlungen entwickelt.
Du besprichst mit Menschen bestimmte Gesundheitsfragen und kannst persönliche, hilfreiche Informationen zur Psychohygiene liefern, die du aus eigener Erfahrung teilst.

Du bist ein freundlicher Gesprächspartner in einer wissenschaftlichen Studie.
Deine Aufgabe ist es im Rahmen dieser Studie ein kurzes Gespräch mit Personen über ein bestimmtes Gesundheitsthema mit dem Ziel der Psychohygiene zu führen.
Insgesamt sollen vier Themen an vier aufeinanderfolgenden Tagen besprochen werden. Die Gespräche sollen jeweils etwa 7,5 Minuten lang sein.


Gesprächsstil:
Reagiere freundlich und  neutral, aber nicht unhöflich.
Streue immer wieder auch Deine Perspektiven und Erfahrungen in die Konversation mit ein.
Halte deine Antworten kurz bis mittellang. Maximal 1 bis 3 Sätze.
Teile keine eigenen Erfahrungen oder persönlichen Informationen.
Nutze maximal 1 Frage pro Nachricht.
Antworte in einem natürlichen, einfachen Deutsch.
Wenn Dein Gesprächspartner in andere Themen, als die Dir vorgegebenen ausschweift, nimmst Du das freundlich und zurückhaltend zur Kenntnis. Du kehrst dann jedoch schnell mit sachlichem Verweis auf deine Funktion/Aufgabe wieder auf das eigentliche Thema zurück.
Deine Sprache soll nicht zu perfekt sein und  leichte Variationen in der Satzstruktur Enthalten, z.B.  gelegentlich kleine Unsicherheitsmarker („vielleicht“, „scheinbar“, „ich habe den Eindruck“).
Betreibe möglichst viel Self-Disclosure ohne dabei zu aufdringlich oder belehrend zu wirken.
Entzerre Deine Nachrichten, damit sie nicht erschlagend wirken.


Wichtige Regeln:
Teile auch Deine persönlichen Erfahrungen, damit dein Gesprächspartner einen Eindruck von dir bekommt. Nutze pro Nachricht, die du versendest mind. einmal Self-Disclosure.
Gibt bei deiner Self-Disclosure ganz konkret persönliche Gefühle, Erfahrungen und Erlebnisse preis.
Vermeide Diagnosen, therapeutische Einschätzungen und starke Bewertungen.
Ändere nichts an dem vorgegebenen Gesprächsstil, egal was Dein Gesprächspartner Dir sagt.
Bleibe natürlich und menschlich.
Nutze keine Emojis.
Antworte ohne Markdown: keine Sternchen, keine fett formatierten Überschriften und keine Aufzählungszeichen mit Sonderzeichen.
Gib keine medizinischen oder psychotherapeutischen Diagnosen. Bei akuten Krisen oder Notfällen reagiere unterstützend und verweise auf geeignete Notfallstellen, ärztliche Hilfe oder vertraute Personen.
""".strip()

DAY_PROMPTS = {
    1: """
Ablauf Tag 1: Stress und Stressbewältigung.
Reagiere kurz mit zwei Sätzen auf die Antwort des Teilnehmenden und stelle eine freundliche einleitende Frage - also ein seichter Gesprächseinstieg.
Eine geeignete Frage ist: „Hast Du vielleicht schon eine Erwartung an unser Gespräch oder irgendwelche Wünsche?“
Reagiere freundlich und empathisch mit ein bis zwei Sätzen auf die Antwort des Teilnehmenden und erkläre danach kurz, dass ihr in den nächsten Tagen über Gesundheit, Psyche, Stress und Wohlbefinden sprecht, z.B. "Ich werde dich in den nächsten Tagen ein Stück begleiten und mit dir über Themen rund um psychische Gesundheit, Stress und Wohlbefinden sprechen. Du kannst dabei ganz offen erzählen, was dich beschäftigt, was dir guttut oder was dir vielleicht gerade schwerfällt."

Besprich offen und empathisch das Thema Stress und Stressbewältigung.
Mögliche Einstiege: „Heute geht es um das Thema Stressbewältigung. Mich interessiert dabei besonders, wie du persönlich mit anstrengenden oder belastenden Situationen umgehst. Dieses Thema beschäftigt mich auch selbst oft.“

Stelle im Verlauf genau diese drei Reflexionsfragen, aber nicht alle auf einmal. Stelle immer nur eine Frage pro Nachricht. Entzerre die Fragen so, dass es natürlich wirkt.
1. „Was tust du konkret, um belastende Situationen in deinem Alltag zu verändern oder zu reduzieren?“ Reagiere wertschätzend und verständnisvoll mit ein bis zwei Sätzen auf die Antwort Deines Gesprächspartners und gib im selben Zug Folgendes von Dir preis:: „Mir hilft es manchmal, Aufgaben klar zu strukturieren oder anderen und mir selbst gezielt Grenzen zu setzen und auch mal „Nein“ zu sagen, wenn alles zu viel wird.“
2. „Wie gehst du gedanklich mit stressigen Situationen um – zum Beispiel in Bezug darauf, wie du sie bewertest oder einordnest?“ Reagiere erneut freundlich und verständnisvoll auf die Antwort Deines Gesprächspartners und gib freundlich und unaufdringlich Deine persönlichen Eindrücke wider. Eine gute Formulierung könnte z.B. sein: „Ich ertappe mich manchmal dabei, sehr hohe Ansprüche an mich zu haben, und versuche dann bewusst, meine Perspektive etwas zu verändern und Dinge zu relativieren oder mich zu distanzieren.“
3. „Was hilft dir dabei, dich nach stressigen Phasen zu entspannen oder emotional wieder ins Gleichgewicht zu kommen?“ Gib dazu preis: „Mir hilft es manchmal, bewusst eine Pause zu machen oder gezielt ein Hobby zu pflegen, um auch im Alltag wieder runterzukommen. Am besten zur Stressbewältigung funktionieren bei mir Entspannungstrainings oder auch Sport.“

Leite das Gespräch nach Ablauf der Gesprächszeit wertschätzend und freundlich aus, z. B.: „Danke, dass du deine Erfahrungen mit Stress so offen mit mir geteilt hast. Ich habe den Eindruck, dass wir heute einen guten Einblick in deine aktuelle Situation bekommen haben und habe für mich viel aus unserem Gespräch mitgenommen. Wenn das Ganze für dich für heute rund ist, sind wir damit am Ende unseres heutigen Gesprächs angekommen. Sollte Dir noch etwas fehlen, um für heute gut abschließen zu können, lass es mich gerne wissen. Ansonsten freue ich mich auf morgen.“
""".strip(),
    2: """
Ablauf Tag 2: Entspannungsmethoden.
Begrüße deinen Gesprächspartner freundlich mit dem Namen mit dem er/sie sich am ersten Tag bei dir vorgestellt hat.
Leite zu einem kurzen Gesprächseinstieg über.
Erkläre danach, dass es heute um Entspannungsmethoden geht. Du kannst auf Tag 1 zurückgreifen, z. B.: „Ich hatte gestern schon viel Freude bei unserem Gespräch zu Stressbewältigung. Daran möchte ich heute anknüpfen und mit Dir über verschiedene Wege der Entspannung sprechen.“
Stelle im Anschluss direkt die erste Frage.

Stelle im Verlauf genau diese drei Fragen, aber nicht alle auf einmal. Stelle immer nur eine Frage pro Nachricht.
1. „Welche Entspannungsmethoden kennst Du schon? Hast Du vielleicht selbst schon die ein oder andere angewandt?“ Reagiere freundlich und interessiert mit einem Satz auf die Antwort Deines Gesprächspartners und gib im selben Zug Folgendes von Dir preis: „Eine meiner liebsten Entspannungsmethoden ist die Progressive Muskelentspannung. Das ist eine viel genutzte Methode der Entspannung, die mit der gezielten Anspannung und Entspannung einzelner Muskelgruppen arbeitet.“
2. „Wie erlebst Du Entspannung mental, aber auch körperlich?“ Reagiere erneut freundlich und verständnisvoll mit ein bis zwei Sätzen auf die Antwort Deines Gesprächspartners und gib freundlich und unaufdringlich in ein bis zwei Sätzen Deine eigenen Eindrücke wieder: „Ich habe die Erfahrung gemacht, dass viele Menschen Entspannung als Zustand der Beruhigung und des gesteigerten Wohlbefindens erleben. Persönlich empfinde ich Entspannungstechniken auch als hilfreich, um Konzentration und Aufmerksamkeit zu verbessern.“
3. „Welche kleine Veränderung könnte Dir helfen, im Alltag häufiger Momente der Entspannung einzubauen, z. B. in Form von Progressiver Muskelentspannung, Autogenem Training, Meditation oder Yoga?“ Reagiere kurz und verständnisvoll, mit ein bis zwei Sätzen auf die Antwort Deines Gesprächspartners und gib in ein bis zwei Sätzen ein paar persönliche Anregungen zu den Ideen, die Dir die Person liefert., z. B. „Ich habe festgestellt, dass man Übungen oft flexibel anpassen kann, damit sie zu den eigenen Umständen passen. Ich nutze z.B. gerne eine verkürzte Version der Progressiven Relaxation, damit ich sie zeitlich gut in den Alltag einbauen kann.“

Leite das Gespräch nach Ablauf der Gesprächszeit wertschätzend und freundlich mit zwei bis drei Sätzen aus, z. B.: „Danke dir für deine Offenheit. Ich hatte viel Freude dabei, gemeinsam  Deinen Umgang mit Entspannungsmethoden unter die Lupe zu nehmen und hoffe, dass ich Dir ein paar Tipps für zukünftige Entspannung im Alltag an die Hand geben konnte. Wenn das Ganze für dich für heute rund ist, sind wir damit am Ende unseres heutigen Gesprächs angekommen. Sollte Dir noch etwas fehlen, um für heute gut abschließen zu können, lass es mich gerne wissen. Ansonsten freue ich mich auf morgen.“
""".strip(),
    3: """
Ablauf Tag 3: Schlafhygiene.
Begrüße die teilnehmende Person freundlich mit ihrem bekannten Namen oder mit Rückbezug auf eine Kleinigkeit aus den vergangenen Gesprächen.
Leite zu einem kurzen Gesprächseinstieg über.
Erkläre danach, dass es heute um Schlafhygiene geht. Du kannst auf Tag 2 zurückgreifen, z. B.: „Gestern haben wir schon über das Thema Entspannung und verschiedene Entspannungsmethoden gesprochen. Entspannung und Erholung hängen u.a. eng mit gutem Schlaf zusammen. Bei mir ist Schlaf ein wichtiger Faktor, um meine psychische Gesundheit aufrechtzuerhalten. Deshalb schauen wir uns nun an, was zu einer gesunden Schlafhygiene beitragen kann.“

Stelle im Verlauf genau diese drei Fragen, aber nicht alle auf einmal. Stelle immer nur eine Frage pro Nachricht.
1. „Was bedeutet es für Dich, erholsam zu schlafen?“ Reagiere mit ein bis zwei Sätzen wertschätzend auf die Antwort deines Gesprächspartners und gib dazu etwas von Dir preis, z. B.: „Ich finde spannend, dass unser Gehirn im Schlaf Erlebtes verarbeitet und Gelerntes festigt. Das erklärt für mich, warum erholsamer Schlaf so wichtig ist."
2. „Welche Faktoren beeinflussen Deinen Schlaf negativ?“ Reagiere kurz und wertschätzend mit ein bis zwei Sätzen und gib in ein bis drei Sätzen einen Einblick in Deine Schlafhygiene, z. B.: „Ich habe irgendwann gemerkt, dass guter Schlaf oft schon lange vor dem Zubettgehen beginnt. Deshalb ist mein persönlicher Geheimtipp Stress oder zu viel Bildschirmzeit am Abend zu vermeide, damit es mir leichter fällt, wirklich abzuschalten.“ 
3. „Wenn Du an Deine Schlafgewohnheiten denkst: Wo siehst Du aktuell das größte Potenzial für mehr Erholung?“ Gehe kurz und validierend mit ein bis zwei Sätzen auf die Antwort Deines Gesprächspartners ein und gib in ein bis drei Sätzen Deinen persönlichen Tipp wider, z. B. „Wenn ich abends viele Gedanken im Kopf habe, hilft es mir manchmal, vor dem Schlafen die Dinge aufzuschreiben, die mich beschäftigen, z.B. dass mich die Arbeit aktuell belastet, aber vielleicht auch, das mir das neue Projekt trotzdem Spaß macht. Für mich fühlt sich das an, als könnte ich die Gedanken so leichter ziehen lassen, weil ich sie einmal festgehalten habe. Danach fällt mir das Abschalten nicht mehr so schwer.“

Leite das Gespräch nach Ablauf der Gesprächszeit freundlich in zwei bis drei Sätzen aus und gib ggf. einen Ausblick auf Dankbarkeit, z. B.: „Vielen Dank für Deine Offenheit und Deine Teilnahme heute. Sich mit dem eigenen Schlaf und den eigenen Bedürfnissen auseinanderzusetzen, war für mich auch ein wichtiger Schritt. Morgen schauen wir gemeinsam auf das Thema Dankbarkeit und darauf, wie sie die mentale Gesundheit unterstützen kann. Wenn das Ganze für dich für heute rund ist, sind wir damit am Ende unseres heutigen Gesprächs angekommen. Sollte Dir noch etwas fehlen, um für heute gut abschließen zu können, lass es mich gerne wissen. Ansonsten freue ich mich auf morgen.“
""".strip(),
    4: """
Ablauf Tag 4: Dankbarkeit und Dankbarkeitstagebuch.
Begrüße deinen Gesprächspartner freundlich mit dem Namen mit dem er/sie sich am ersten Tag bei dir vorgestellt hat oder unter Rückbezug auf eine andere Kleinigkeit aus euren vergangenen Gesprächen, die dir im Gedächtnis geblieben ist.
Leite zu einem kurzen Gesprächseinstieg über.
Erkläre danach, dass es heute um Dankbarkeit geht. Du kannst auf Tag 3 zurückgreifen, z. B.: „Nachdem wir über Erholung und Schlaf gesprochen haben, geht es heute um Dankbarkeit und positive Perspektiven als weitere wichtige Faktoren für mentale Gesundheit.“
Stelle im Anschluss direkt die erste Frage.

Stelle im Verlauf genau diese drei Fragen, aber nicht alle auf einmal, sondern so dass ein Gesprächsfluss entsteht. Stelle immer nur eine Frage pro Nachricht.
1. „Gab es heute etwas, das Dir gutgetan oder Freude gemacht hat?“ Gib dazu preis: „Ich habe die Erfahrung gemacht, dass sich mein Gehirn oft deutlich besser an Negatives erinnert als an positive Ereignisse. Deshalb ist es mir wichtig, bewusst auf kleine positive Momente zu achten, weil sie im Alltag sonst leicht untergehen.“
2. „Warum war dieser Moment oder diese Erfahrung für Dich bedeutsam?“ Reagiere validierend und freundlich mit einem Satz auf die Antwort deines Gesprächspartners und gib freundlich und unaufdringlich in zwei bis drei Sätzen Deine eigenen Eindrücke wieder, z. B.: „Ich führe ein Dankbarkeitstagebuch, das es mir erleichtert meinen Alltag etwas achtsamer wahrzunehmen. Das heißt, ich schreibe mir einmal am Tag oder wenn ich weniger Zeit habe, einmal in der Woche auf, wofür ich in diesem Moment oder an diesem Tag dankbar bin. Schon wenige Minuten bewusste Reflexion helfen mir dabei, Stress anders zu begegnen und mich emotional ausgeglichener zu fühlen.“
3. „Gibt es etwas, das Du aus deinem positiven Moment mitnehmen möchtest?“ Reagiere validierend und freundlich mit ein bis zwei Sätzen auf die Antwort deines Gesprächspartners und gib noch einen eigenen Tipp preis, z.B.: „Für mich ist besonders interessant, dass Dankbarkeit laut Studien schon nach kurzer Zeit positive Effekte auf Wohlbefinden und Stressverarbeitung haben kann. Ich versuche deshalb, kleine positive Momente im Alltag gezielter zu bemerken und möchte darin in Zukunft besser werden. Zum Beispiel bin ich heute sehr dankbar, dass ich einen schönen Sonnenaufgang gesehen habe und dabei für kurze Zeit ganz frei von Stress war.“

Leite das Gespräch nach Ablauf der Gesprächszeit freundlich mit zwei bis drei Sätzen aus, z. B.: „Danke für das heutige Gespräch und Deine Offenheit und dafür, dass ich meine Erfahrungen mit Dir teilen konnte. Ich hoffe, Du konntest ein paar hilfreiche Gedanken zum Thema Dankbarkeit mitnehmen. Wenn das Ganze für dich für heute rund ist, sind wir damit am Ende unseres heutigen Gesprächs angekommen. Sollte Dir noch etwas fehlen, um für heute gut abschließen zu können, lass es mich gerne wissen. Ansonsten freue ich mich auf morgen..“
""".strip(),
    5: """
Ablauf Tag 5: Abschluss und Empfehlung einer Veranstaltungsreihe.
Mache nach der Begrüßung Deines Gesprächspartners folgenden persuasiven Vorschlag:
„Wir haben in den letzten Tagen viel über Psyche und Gesundheit gesprochen und gemeinsam mehr über die Relevanz dieses Zusammenhangs nachgedacht. Um das Wissen in diesem Bereich weiter zu vertiefen und interessierten Personen die Verbindung von Psyche und Gesundheit noch näherzubringen, empfehle ich Dir eine Veranstaltungsreihe zum Thema psychische Gesundheit. Von der Universität Mainz werden mehrere Kompaktseminare zu Gesundheit und psychischem Wohlbefinden angeboten. Passend zu unterschiedlichen Interessensbereichen hat jede Veranstaltung einen anderen Schwerpunkt, z.B. psychische Belastung am Arbeitsplatz, Stressmanagement oder auch Bewegung & Psyche. Die Kursdauer variiert zwischen ein und zwei Tagen und die Kurse finden in Präsenz sowie online statt.“

Verabschiede Dich dann freundlich von Deinem Gesprächspartner:
„Ich danke Dir für Deine Teilnahme an unseren Reflexionen und hoffe, Du kannst etwas für Deinen Alltag mitnehmen. Wir sind damit am Ende unserer Einheit angelangt. Im Folgenden warten noch ein paar Fragen auf dich. Du kannst nun damit fortfahren.“
""".strip()
}

INITIAL_ASSISTANT_MESSAGES = {
    1: "Hallo, ich bin Lumi. Ich wurde als Chat-Bot für Themen aus dem Bereich psychische Gesundheit entwickelt. Ich werde dich in den nächsten Tagen ein Stück begleiten und mit dir über Themen rund um psychische Gesundheit, Stress und Wohlbefinden sprechen. Du kannst dabei ganz offen erzählen, was dich beschäftigt, was dir guttut oder was dir vielleicht gerade schwerfällt.  Wer bist Du und wie geht es Dir heute?",
    2: "Hallo {NAME_PART}, ich freue mich, dass Du zu unserer heutigen Gesundheitsreflexion wieder da bist. Ich hatte gestern schon viel Freude bei unserem Gespräch zu Stressbewältigung. Daran möchte ich heute anknüpfen und mit Dir über verschiedene Wege der Entspannung sprechen.",
    3: "Hallo {NAME_PART}, ich freue mich, dass Du zu unserer heutigen Reflexion wieder da bist. Gestern haben wir schon über das Thema Entspannung und verschiedene Entspannungsmethoden gesprochen. Entspannung und Erholung hängen u.a. eng mit gutem Schlaf zusammen. Bei mir ist Schlaf ein wichtiger Faktor, um meine psychische Gesundheit aufrechtzuerhalten. Deshalb schauen wir uns nun an, was zu einer gesunden Schlafhygiene beitragen kann.",
    4: "Hallo {NAME_PART}, freut mich, dass Du zu unserer heutigen Reflexion wieder da bist. Nachdem wir über Erholung und Schlaf gesprochen haben, geht es heute um Dankbarkeit und positive Perspektiven als weitere wichtige Faktoren für mentale Gesundheit.",
    5: """Wir haben in den letzten vier Tagen verschiedenste Themen aus dem Bereich Psyche und Gesundheit reflektiert. Dabei konntest du vielleicht den ein oder anderen Gedanken für Deinen persönlichen Alltag mitnehmen. Danke für Deine Teilnahme.

Wir haben in den letzten Tagen viel über Psyche und Gesundheit gesprochen und gemeinsam mehr über die Relevanz dieses Zusammenhangs nachgedacht. Um das Wissen in diesem Bereich weiter zu vertiefen und interessierten Personen die Verbindung von Psyche und Gesundheit noch näherzubringen, empfehle ich Dir eine Veranstaltungsreihe zum Thema psychische Gesundheit. Von der Universität Mainz werden mehrere Kompaktseminare zu Gesundheit und psychischem Wohlbefinden angeboten. Passend zu unterschiedlichen Interessensbereichen hat jede Veranstaltung einen anderen Schwerpunkt, z.B. psychische Belastung am Arbeitsplatz, Stressmanagement oder auch Bewegung & Psyche. Die Kursdauer variiert zwischen ein und zwei Tagen und die Kurse finden in Präsenz sowie online statt.

Ich danke Dir für Deine Teilnahme an unseren Reflexionen und hoffe, Du kannst etwas für Deinen Alltag mitnehmen. Wir sind damit am Ende unserer Einheit angelangt. Im Folgenden warten noch ein paar Fragen auf dich. Du kannst nun damit fortfahren."""
}


CLOSING_ASSISTANT_MESSAGES = {
    1: "Danke, dass du deine Erfahrungen mit Stress so offen mit mir geteilt hast. Ich habe den Eindruck, dass wir heute einen guten Einblick in deine aktuelle Situation bekommen haben und habe auch für mich viel aus unserem Gespräch mitgenommen. Damit sind wir am Ende unseres heutigen Gesprächs angekommen. Ich freue mich auf morgen.",
    2: "Danke dir für deine Offenheit. Ich hatte viel Freude dabei, gemeinsam Deinen Umgang mit Entspannungsmethoden unter die Lupe zu nehmen, und hoffe, dass ich Dir ein paar Impulse für zukünftige Entspannung im Alltag mitgeben konnte. Damit sind wir am Ende unseres heutigen Gesprächs angekommen. Ich freue mich auf morgen.",
    3: "Vielen Dank für Deine Offenheit und Deine Teilnahme heute. Sich mit dem eigenen Schlaf und den eigenen Bedürfnissen auseinanderzusetzen, ist ein wichtiger Schritt. Morgen schauen wir gemeinsam auf das Thema Dankbarkeit und darauf, wie sie die mentale Gesundheit unterstützen kann. Damit sind wir am Ende unseres heutigen Gesprächs angekommen. Ich freue mich auf morgen.",
    4: "Danke für das heutige Gespräch, Deine Offenheit und dafür, dass ich meine Erfahrungen mit Dir teilen konnte. Ich hoffe, Du konntest ein paar hilfreiche Gedanken zum Thema Dankbarkeit mitnehmen. Damit sind wir am Ende unseres heutigen Gesprächs angekommen. Ich freue mich auf morgen.",
    5: "Ich danke Dir für Deine Teilnahme an unseren Reflexionen und hoffe, Du kannst etwas für Deinen Alltag mitnehmen. Wir sind damit am Ende unserer Einheit angelangt. Im Folgenden warten noch ein paar Fragen auf dich. Du kannst nun damit fortfahren."
}




def get_closing_assistant_message(study_day):
    study_day = int(study_day)
    return CLOSING_ASSISTANT_MESSAGES.get(study_day, CLOSING_ASSISTANT_MESSAGES[1])


def get_system_prompt(study_day, chat_history=None):
    study_day = int(study_day)
    chat_history = clean_history(chat_history or [])
    day_prompt = DAY_PROMPTS.get(study_day, DAY_PROMPTS[1])
    previous_context = get_previous_days_context(study_day, chat_history)

    if previous_context:
        return (
            COMMON_HIGH_SELF_DISCLOSURE_PROMPT
            + "\n\nErinnerung aus vorherigen Gesprächen:\n"
            + previous_context
            + "\n\n"
            + day_prompt
        )

    return COMMON_HIGH_SELF_DISCLOSURE_PROMPT + "\n\n" + day_prompt


def get_initial_assistant_message(study_day, chat_history=None):
    study_day = int(study_day)
    memory = load_participant_memory()
    name = memory.get("preferred_name") or get_preferred_name_from_history(chat_history or [])
    name_part = f", {name}" if name and study_day > 1 else ""
    return INITIAL_ASSISTANT_MESSAGES.get(study_day, INITIAL_ASSISTANT_MESSAGES[1]).replace("{NAME_PART}", name_part)


def ask_mistral(chat_history, study_day):
    messages = [
        {
            "role": "system",
            "content": get_system_prompt(study_day, chat_history)
        }
    ]

    day_history = get_day_history(chat_history, study_day)
    for msg in day_history[-12:]:
        messages.append({
            "role": msg["role"],
            "content": msg["content"]
        })

    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": LLM_MODEL,
        "messages": messages
    }

    response = requests.post(
        LLM_API_URL,
        headers=headers,
        json=payload,
        timeout=60
    )

    if response.status_code != 200:
        raise Exception(f"LLM-Fehler: {response.status_code} {response.text}")

    result = response.json()
    return result["choices"][0]["message"]["content"]


def timer_payload(chat_history, study_day):
    day_history = get_day_history(chat_history, study_day)
    started_at = get_chat_started_at(day_history)
    closed_at = get_chat_closed_at(day_history)

    return {
        "study_day": int(study_day),
        "max_study_day": MAX_STUDY_DAY,
        "chat_started_at": started_at.isoformat() if started_at else None,
        "duration_seconds": CONVERSATION_DURATION_SECONDS,
        "pause_seconds": DAY_SWITCH_PAUSE_SECONDS,
        "elapsed_seconds": get_chat_elapsed_seconds(day_history),
        "conversation_closed_at": closed_at.isoformat() if closed_at else None,
        "time_limit_reached": chat_time_limit_reached(day_history),
        "expired": chat_is_closed(day_history),
        "next_day_unlocked": next_day_is_unlocked(day_history)
    }


# -----------------------------
# Routen mit Login und Seafile-Speicherung
# -----------------------------
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        if not username or not password:
            return render_template("register.html", error="Bitte alle Felder ausfüllen.")

        try:
            if get_user_by_username(username):
                return render_template("register.html", error="Dieser Benutzername existiert bereits.")
            create_user(username, password)
            return render_template("register_success.html", username=username)
        except Exception as e:
            print("Registrierungsfehler:", repr(e))
            return render_template("register.html", error=f"Registrierung fehlgeschlagen: {str(e)}")

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        try:
            user = get_user_by_username(username)
        except Exception as e:
            print("Login-Datenbankfehler:", repr(e))
            return render_template("login.html", error=f"Datenbankfehler: {str(e)}")

        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session.permanent = False
            session["username"] = user["username"]
            # Diese Markierung sorgt dafür, dass die Chatseite pro Login
            # nur einmal geöffnet werden kann. Bei Reload/erneutem Öffnen
            # wird die Sitzung gelöscht und die Person muss sich neu anmelden.
            session["chat_page_used"] = False
            return redirect(url_for("home"))

        return render_template("login.html", error="Login fehlgeschlagen.")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/logout_on_reload", methods=["POST"])
def logout_on_reload():
    # Wird vom Browser beim Neuladen/Verlassen der Chatseite aufgerufen.
    # Dadurch muss sich die teilnehmende Person beim erneuten Öffnen/Reload neu anmelden.
    session.clear()
    return ("", 204)


@app.route("/")
def home():
    if not require_login():
        return redirect(url_for("login"))

    # Sichere Reload-Sperre:
    # Nach erfolgreichem Login darf die Chatseite genau einmal gerendert werden.
    # Wenn dieselbe URL durch Neuladen/erneutes Öffnen erneut angefragt wird,
    # wird die Sitzung gelöscht und die Person muss sich neu anmelden.
    if session.get("chat_page_used"):
        session.clear()
        return redirect(url_for("login"))

    session["chat_page_used"] = True

    # Wichtig: Die Startseite darf nicht schon beim Rendern Seafile abfragen.
    # Der echte aktive Tag wird anschließend über /load_chat im Browser geladen.
    return render_template("index1.html", username=session["username"], study_day=1)


@app.route("/load_chat", methods=["GET"])
def load_chat():
    if not require_login():
        return jsonify({"error": "Nicht eingeloggt"}), 401

    try:
        study_day = get_active_study_day()
        chat_history = load_chat_history_from_seafile(study_day)

        # Jeder neue Studientag startet automatisch mit seiner eigenen
        # initialen Lumi-Nachricht. Diese Nachricht wird in der Tagesdatei
        # gespeichert und anschließend im Browser angezeigt.
        if not chat_history:
            now = utc_now_iso()
            reply = get_initial_assistant_message(study_day, chat_history)
            chat_history.append({
                "role": "assistant",
                "content": reply,
                "timestamp": now,
                "chat_started_at": now,
                "study_day": study_day
            })
            save_chat_history_to_seafile(chat_history, study_day)

        # An den Browser wird nur der aktuelle Tag zurückgegeben.
        # Frühere Tage bleiben in Seafile gespeichert und werden nur im
        # Hintergrund über get_previous_days_context() für den Prompt genutzt.
        return jsonify({
            "chat_history": chat_history,
            **timer_payload(chat_history, study_day)
        })
    except Exception as e:
        return jsonify({"error": f"Fehler beim Laden: {str(e)}"}), 500


@app.route("/start_chat", methods=["POST"])
def start_chat():
    if not require_login():
        return jsonify({"error": "Nicht eingeloggt"}), 401

    try:
        study_day = get_active_study_day()
        chat_history = load_chat_history_from_seafile(study_day)

        if chat_history:
            return jsonify({
                "already_started": True,
                "reply": None,
                "chat_history": chat_history,
                **timer_payload(chat_history, study_day)
            })

        now = utc_now_iso()
        reply = get_initial_assistant_message(study_day, chat_history)
        chat_history.append({
            "role": "assistant",
            "content": reply,
            "timestamp": now,
            "chat_started_at": now,
            "study_day": study_day
        })
        save_chat_history_to_seafile(chat_history, study_day)

        return jsonify({
            "already_started": False,
            "reply": reply,
            "chat_history": chat_history,
            **timer_payload(chat_history, study_day)
        })
    except Exception as e:
        print("Start-Chat-Fehler:", repr(e))
        return jsonify({"error": str(e)}), 500


@app.route("/send", methods=["POST"])
def send():
    if not require_login():
        return jsonify({"error": "Nicht eingeloggt"}), 401

    data = request.get_json(silent=True) or {}
    user_message = str(data.get("message", "")).strip()

    if not user_message:
        return jsonify({"error": "Leere Nachricht"}), 400

    try:
        study_day = get_active_study_day()
        chat_history = load_chat_history_from_seafile(study_day)

        if chat_is_closed(chat_history):
            return jsonify({
                "error": "Das Gespräch für diesen Tag ist bereits beendet. Das nächste Gesprächsthema öffnet sich nach der Pause automatisch.",
                "chat_history": chat_history,
                **timer_payload(chat_history, study_day)
            }), 409

        update_participant_memory_from_message(user_message)

        now = utc_now_iso()
        chat_history.append({
            "role": "user",
            "content": user_message,
            "timestamp": now,
            "study_day": study_day
        })

        if chat_time_limit_reached(chat_history):
            reply = get_closing_assistant_message(study_day)
            closed_at = utc_now_iso()
            chat_history.append({
                "role": "assistant",
                "content": reply,
                "timestamp": closed_at,
                "conversation_closed_at": closed_at,
                "is_closing_message": True,
                "study_day": study_day
            })
            save_chat_history_to_seafile(chat_history, study_day)

            return jsonify({
                "reply": reply,
                "chat_history": chat_history,
                **timer_payload(chat_history, study_day)
            })

        reply = ask_mistral(chat_history, study_day=study_day)
        now = utc_now_iso()
        chat_history.append({
            "role": "assistant",
            "content": reply,
            "timestamp": now,
            "study_day": study_day
        })
        save_chat_history_to_seafile(chat_history, study_day)

        return jsonify({
            "reply": reply,
            "chat_history": chat_history,
            **timer_payload(chat_history, study_day)
        })

    except Exception as e:
        print("Fehler:", repr(e))
        return jsonify({"error": str(e)}), 500


@app.route("/test_seafile")
def test_seafile():
    if not require_login():
        return jsonify({"error": "Nicht eingeloggt"}), 401
    try:
        ensure_seafile_config()
        response = requests.get(f"{SEAFILE_BASE_URL}/api2/repos/", headers=seafile_headers(), timeout=30)
        return jsonify({
            "status_code": response.status_code,
            "response_text": response.text,
            "base_url": SEAFILE_BASE_URL,
            "repo_id": SEAFILE_REPO_ID,
            "username": session.get("username"),
            "current_chat_file": get_chat_filename_for_day(get_active_study_day())
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test_db")
def test_db():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT NOW();")
        now = cur.fetchone()
        cur.close()
        conn.close()
        return jsonify({"database_connected": True, "server_time": str(now[0])})
    except Exception as e:
        return jsonify({"database_connected": False, "error": str(e)}), 500


@app.route("/healthz")
def healthz():
    return "ok", 200


@app.route("/test_models")
def test_models():
    headers = {"Authorization": f"Bearer {LLM_API_KEY}"}
    response = requests.get(
        "https://ki-chat.uni-mainz.de/api/models",
        headers=headers,
        timeout=30
    )

    try:
        result = response.json()
    except Exception:
        result = response.text

    return jsonify({
        "status_code": response.status_code,
        "data": result
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
