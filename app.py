from flask import Flask, request, jsonify, render_template
from werkzeug.middleware.proxy_fix import ProxyFix
import os
import sqlite3
import statistics
import anthropic
from dotenv import load_dotenv
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
load_dotenv()

app = Flask(__name__)

# Hinter einem Reverse Proxy (z.B. Caddy) die echte Client-IP aus X-Forwarded-For nehmen.
# Nur aktivieren, wenn die App ausschließlich über den Proxy erreichbar ist –
# sonst kann jeder den Header fälschen und das Rate-Limit umgehen.
if os.getenv('TRUST_PROXY') == '1':
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Begrenzt API-Kosten durch Missbrauch: Anfragen pro IP
limiter = Limiter(get_remote_address, app=app, storage_uri='memory://')

DB_PATH = 'hyrox.db'
MAX_INPUT_LENGTH = 300
client = anthropic.Anthropic()  # liest ANTHROPIC_API_KEY aus environment

TIME_COLUMNS = (
    ['total_time', 'work_time', 'roxzone_time', 'run_time'] +
    [f'run_{i}' for i in range(1, 9)] +
    [f'work_{i}' for i in range(1, 9)] +
    [f'roxzone_{i}' for i in range(1, 9)]
)

SCHEMA = """
Tabelle: results
Spalten:
- event_id, event_name (Text)
- gender: 'male' oder 'female'
- nationality: 3-Buchstaben ISO-Code (z.B. GER, AUT, GBR, USA)
- division: 'open', 'pro', 'doubles', 'relay', 'elite', 'goruck', 'goruck_doubles'
- age_group (Text) – exakte Werte je Division:
    open, pro, goruck:  '16-24', '25-29', '30-34', '35-39', '40-44', '45-49', '50-54', '55-59', '60-64', '65-69', '70-74', '75-79', '80-84'
    doubles:            '16-29', '30-39', '40-49', '50-59', '60-69', 'M60+', 'W60+'
    relay:              'U40', '40+', '40-49', 'W40+', 'M40+', 'X40+'
    elite:              keine Age Groups
- total_time, work_time, roxzone_time, run_time: Text, Format H:MM:SS
- run_1 bis run_8, work_1 bis work_8, roxzone_1 bis roxzone_8: Splits als Text
"""


def time_to_seconds(time_str):
    if not time_str or not isinstance(time_str, str):
        return None
    parts = time_str.strip().split(':')
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
    except (ValueError, IndexError):
        pass
    return None


def seconds_to_time(seconds):
    if seconds is None:
        return None
    seconds = int(round(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f'{h}:{m:02d}:{s:02d}'


def compute_time_stats(results):
    """Returns per-column dicts with mean and median in seconds and H:MM:SS."""
    stats = {}
    present_cols = [c for c in TIME_COLUMNS if any(c in row for row in results)]
    for col in present_cols:
        values = [time_to_seconds(row[col]) for row in results if col in row]
        values = [v for v in values if v is not None]
        if not values:
            continue
        mean_s = statistics.mean(values)
        median_s = statistics.median(values)
        stats[col] = {
            'n': len(values),
            'mean_s': round(mean_s, 1),
            'mean': seconds_to_time(mean_s),
            'median_s': round(median_s, 1),
            'median': seconds_to_time(median_s),
        }
    return stats


@app.route('/')
def index():
    return render_template('index.html')

@app.errorhandler(429)
def rate_limit_exceeded(e):
    return jsonify({"error": "Zu viele Anfragen. Bitte versuche es später noch einmal."}), 429


@app.route('/query', methods=['POST'])
@limiter.limit('10 per hour')
def query():
    data = request.get_json(silent=True) or {}
    user_input = data.get('message')
    if not isinstance(user_input, str) or not user_input.strip():
        return jsonify({"error": "Bitte gib eine Frage ein."}), 400
    user_input = user_input.strip()
    if len(user_input) > MAX_INPUT_LENGTH:
        return jsonify({"error": f"Die Frage darf höchstens {MAX_INPUT_LENGTH} Zeichen lang sein."}), 400

    # Schritt 1: Claude generiert SQL
    sql_response = client.messages.create(
        model="claude-sonnet-5-5",
        thinking={"type": "between_tools"},  # kein Thinking, damit content[0] Text bleibt
        max_tokens=2000,
        messages=[{
            "role": "user",
            "content": f"""Du bist ein SQLite-Experte. Gegeben dieses Schema:
{SCHEMA}

Generiere NUR ein SQLite-kompatibles SQL-Query für folgende Anfrage:
{user_input}

WICHTIG:
- Genau ein einziges SQLite-Statement, kein Semikolon am Ende
- CTEs sind erlaubt: WITH ... AS (...) SELECT ... zählt als ein Statement
- Keine mehrfachen Statements, kein UNION über separate Queries hinaus
- Nur SQLite-Syntax, kein PostgreSQL
- age_group exakt mit den oben angegebenen Werten filtern, kein LIKE
- Zeiten sind als Text im Format H:MM:SS gespeichert – KEIN AVG(), KEIN MIN(), KEIN MAX() auf Zeitspalten, kein CASE WHEN für Zeitkonvertierung
- Das Query soll Rohdaten zurückgeben; Zeitberechnungen (Durchschnitt, Median) werden in Python durchgeführt
- Nur das Query, kein Text drumrum, keine Backticks"""
        }]
    )

    sql = sql_response.content[0].text.strip()

    if sql.startswith("```"):
        sql = sql.split("\n", 1)[1]  # erste Zeile weg
        sql = sql.rsplit("```", 1)[0]  # letzte Backticks weg
    sql = sql.strip()
    print("Generated SQL:", sql)

    # Schritt 2: SQL ausführen mit Retry bei Fehler
    results = None
    last_error = None
    for attempt in range(3):
        try:
            # Nur lesend öffnen: generiertes SQL darf die Datenbank nicht verändern
            conn = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True)
            cursor = conn.execute(sql)
            rows = cursor.fetchall()
            columns = [d[0] for d in cursor.description]
            conn.close()
            results = [dict(zip(columns, row)) for row in rows]
            break
        except Exception as e:
            last_error = str(e)
            print(f"SQL-Fehler (Versuch {attempt + 1}): {last_error}\nQuery: {sql}")
            if attempt < 2:
                retry_response = client.messages.create(
                    model="claude-sonnet-5-5",
                    thinking={"type": "between_tools"},  # kein Thinking, damit content[0] Text bleibt
                    max_tokens=2000,
                    messages=[{
                        "role": "user",
                        "content": f"""Das folgende SQLite-Query hat einen Fehler erzeugt:

{sql}

SQLite-Fehlermeldung: {last_error}

Korrigiere das Query. Nur SQLite-kompatible Funktionen verwenden.
Gib nur das korrigierte Query zurück, kein Text drumrum, keine Backticks, kein Semikolon am Ende."""
                    }]
                )
                sql = retry_response.content[0].text.strip()
                if sql.startswith("```"):
                    sql = sql.split("\n", 1)[1]
                    sql = sql.rsplit("```", 1)[0]
                sql = sql.strip()
                print(f"Korrigiertes SQL (Versuch {attempt + 2}): {sql}")

    if results is None:
        return jsonify({"error": last_error, "sql": sql})

    # Schritt 2b: Zeitstatistiken in Python berechnen
    time_stats = compute_time_stats(results)

    stats_text = ""
    if time_stats:
        lines = ["Berechnete Zeitstatistiken (Python):"]
        for col, s in time_stats.items():
            lines.append(
                f"  {col} (n={s['n']}): Durchschnitt {s['mean']} ({s['mean_s']}s), "
                f"Median {s['median']} ({s['median_s']}s)"
            )
        stats_text = "\n".join(lines)

    # Schritt 3: Claude interpretiert Ergebnis
    interpret_response = client.messages.create(
        model="claude-sonnet-5-5",
        thinking={"type": "between_tools"},  # kein Thinking, damit content[0] Text bleibt
        max_tokens=2000,
        messages=[{
            "role": "user",
            "content": f"""Der Benutzer fragte: {user_input}

SQL-Query war: {sql}

Rohdaten (erste 20 Zeilen): {results[:20]}

{stats_text}

Antworte auf Deutsch. Formatierungsregeln:
- Markdown-Tabellen für Vergleiche und tabellarische Daten
- Bullet Points für Listen und Aufzählungen
- Headers (## oder ###) nur wenn die Antwort mehrere Abschnitte hat
- Einfacher Fließtext für kurze, direkte Antworten
- Keine Emojis"""
        }]
    )

    return jsonify({
        "response": interpret_response.content[0].text,
        "sql": sql,
        "row_count": len(results)
    })

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=False)
