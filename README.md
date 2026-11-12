# HYROX Race Data Analysis

Live: https://hyrox.mzeitz.de

A small web app for asking natural-language questions about HYROX race results. The Claude API translates each question into a SQLite query, the query runs against a local results database, and Claude summarises the returned rows.

The question and the answers are in German (the prompts and UI text are German).

## How it works

1. The user submits a question (max. 300 characters) from the web page.
2. Claude generates a single SQLite statement based on the table schema defined in `app.py`.
3. The statement runs against `hyrox.db`, opened read-only. On an SQL error, Claude is asked to correct the query, up to 3 attempts in total.
4. Mean and median for the time columns (stored as `H:MM:SS` text) are computed in Python, not in SQL.
5. Claude receives the question, the SQL, the first 20 rows and the computed statistics, and writes the answer as Markdown.

The endpoint is rate-limited to 10 requests per hour per IP (in-memory, via Flask-Limiter).

## Architecture

- **Backend:** Flask (`app.py`), served with gunicorn in Docker. Routes: `/` (page) and `POST /query`.
- **Frontend:** a single server-rendered page, `templates/index.html`, with plain JavaScript. Markdown answers are rendered with [marked](https://github.com/markedjs/marked), loaded from a CDN. There is no React/Vite build step.
- **Data:** SQLite database `hyrox.db`, one table `results` (about 92,000 rows from 89 events). Columns: event, gender, nationality, division, age group, total/work/roxzone/run times and per-station splits (`run_1`..`run_8`, `work_1`..`work_8`, `roxzone_1`..`roxzone_8`). The data contains no athlete names.
- **`import_csv.py`:** rebuilds the `results` table from a CSV file. The CSV must use the column names above.

## Deployment

The live instance is self-hosted on a Hetzner VPS (Ubuntu). The app runs as a Docker container via Docker Compose, with Flask served by gunicorn. Caddy acts as reverse proxy and handles HTTPS automatically (Let's Encrypt). The container port is bound to localhost only, so the app is reachable from outside only through Caddy.

The image is a single-stage build: there is no frontend build step, and Flask serves the static `templates/index.html` itself. The API key is read from `.env` at runtime (`env_file`) and is not part of the image or the repository.

## Local setup

Requirements: Python 3.12 and an [Anthropic API key](https://console.anthropic.com/).

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # then set ANTHROPIC_API_KEY
python app.py
```

The app runs at http://127.0.0.1:5000.

To use your own data instead of the bundled database:

```bash
python import_csv.py path/to/results.csv            # writes hyrox.db
python import_csv.py path/to/results.csv --db my.db # custom target (DB_PATH in app.py must match)
```

### Docker

```bash
cp .env.example .env   # then set ANTHROPIC_API_KEY
docker compose up --build
```

The container listens on `127.0.0.1:3001`. `docker-compose.yml` sets `TRUST_PROXY=1`, which makes the app read the client IP from `X-Forwarded-For`. Use it only behind a reverse proxy; otherwise the header can be spoofed and the rate limit bypassed.

## Example questions

- Wie schnell war der Median der Männer in der Open-Division in der Altersklasse 30-34?
- Welche Nation hat die schnellste durchschnittliche Gesamtzeit bei den Frauen in der Pro-Division?
- Vergleiche die durchschnittliche Roxzone-Zeit von Open und Pro bei den Männern.
- Wie unterscheiden sich run_1 und run_8 im Durchschnitt bei GER-Athleten?

## Limitations

- Generated SQL is not validated beyond the read-only connection. The model can produce wrong or inefficient queries, and answers are only as good as the generated SQL.
- Only the first 20 result rows are passed to Claude for the answer, although statistics are computed over all returned rows.
- The rate limiter stores counters in memory, so limits reset on restart and are per process.
