# Team Project repo

[![Checks](https://github.com/gcivil-nyu-org/team-5-fall26/actions/workflows/checks.yml/badge.svg?event=pull_request)](https://github.com/gcivil-nyu-org/team-5-fall26/actions/workflows/checks.yml?query=event%3Apull_request)

## Local development

Requires Python 3.11 or newer and Docker with Docker Compose. Start Docker
before running these commands.

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env  # Windows: copy .env.example .env
docker compose up -d --wait db
python manage.py migrate
python manage.py runserver
```

The project uses PostgreSQL 17 locally. Django loads database settings from
`.env`; exported environment variables take precedence. The example credentials
are for local development. Set `POSTGRES_DB`, `POSTGRES_USER`,
`POSTGRES_PASSWORD`, `POSTGRES_HOST`, and `POSTGRES_PORT` to use an existing
PostgreSQL server instead of Docker. Django 5.2 supports PostgreSQL 14 or newer
with the [Psycopg 3 driver](https://docs.djangoproject.com/en/5.2/ref/databases/#postgresql-notes).

The Compose database stores data in a persistent Docker volume. Stop it with
`docker compose stop db` and start it again with `docker compose up -d --wait db`.
Changing the database name, user, or password in `.env` does not update an
already initialized volume; update the existing database to match.

The sign-up page is at http://127.0.0.1:8000/accounts/register/ and the login
page is at http://127.0.0.1:8000/accounts/login/.

In development, emails such as password reset links aren't actually sent;
they're printed in the terminal running `runserver`.

## NYC Parks events

The landing page lists upcoming NYC Parks events imported from the city's
[open data feed](https://data.cityofnewyork.us/api/v3/views/w3wp-dpdi).
Sync the database from the feed with:

```bash
python manage.py sync_events
```

The command fetches every record, creates new events, updates events whose
content changed, leaves unchanged events untouched, and marks events done
once their end time has passed. The feed only publishes a rolling two-week
window, but events stay in the database after they disappear from it.

Run it once to seed your local database.

## Event search

The search bar at the top of the landing page submits to `/search?q=...`.
Search is public and returns upcoming events, ranked by relevance, with 20
results per page. It uses PostgreSQL's English full-text search across event
titles, categories, park names, locations, and descriptions. Titles carry the
highest weight. Quoted phrases, `OR`, and exclusions such as `yoga -kids` are
supported.

Run `python manage.py migrate` to create the search GIN index. The database
automatically maintains the index when events are imported or edited; no
separate indexing command is needed. Result cards link to `/events/{id}`;
event detail pages are a future feature and currently return 404.

## Event filters

A filter bar under the search box narrows results by date, borough, and
category, with or without a search term. Different filters must all match;
choosing several boroughs or categories matches any of them. Dates use New
York time, and an event matches a date range when it overlaps it. The
**Today**, **This weekend**, and **This month** buttons fill in the dates.

For feed events, boroughs come from the first letter of the NYC Parks
property ID, so feed events without one, such as those at community gardens
or libraries, don't appear when a borough is selected. Categories are groups
of the feed's own category names, defined in `events/filters.py`.

## Posting events

Signed-in users can post their own events at `/events/new/`, linked as
**Post an event** on the home page. Hosts choose a borough and categories from
the same lists as the filter bar, so their events appear in search, filters,
and the home page like NYC Parks events. Times are entered in New York time.

Hosts see an **Edit event** button on their own events' detail pages, at
`/events/<id>/edit/`. Only the host can edit an event, and only until it ends;
NYC Parks feed events can't be edited. A start time that was already in the
past can stay as it is, so hosts can still correct events that are under way.

The hourly sync never edits hosted events. It marks them done once they end,
or after the day they start if they have no end time.

## Notifications

While an event is upcoming, its host can message everyone who selected
**Interested** or **Want to Attend** from the event's detail page. The host
sees how many people will get it, but not who they are. Each person gets an
email and an in-app notification. When a host changes an event's date, time, or location,
interested users are told automatically.

Signed-in users see a **Notifications** link on the home page with their
unread count. `/notifications/` lists contact requests first, then event
updates and discussion replies, with the newest items first within each
group. Event updates and reply alerts are marked read when displayed.
Pending contact requests keep their **New** badge and remain in the unread
count until accepted or declined.

Replies to discussion posts notify the original author in-app, except when
replying to your own post. Reply notifications do not send email.

To connect with someone, select **Interested** or **Want to Attend** on an
upcoming event, open a shared attendee profile preview in either group,
optionally add an introduction, and click **Send Contact Request**. The
recipient gets an in-app notification with **Accept** and **Decline** buttons;
they can also respond from the event page. Both actions mark the request
notification read. If the
recipient allows direct contact, the request is automatically accepted and
appears as an already-read introduction. Senders receive no response
notifications, and contact requests do not send emails. Existing pending
requests are included when the notification migrations are applied.

In development, event-update emails are printed to the `runserver` console
instead of being sent.

## Cron job (hourly sync)

The sync is **not automatic yet** — register it once per machine:

```bash
crontab -e
# add this line (absolute paths only; cron has no repo dir and no venv on PATH):
0 * * * * cd /path/to/team-5-fall26 && /path/to/.venv/bin/python manage.py sync_events >> /var/log/team5_sync.log 2>&1
```

- Runs every hour at :00; each run prints a summary to the log file.
- On failure (feed down, bad payload) exits non-zero and leaves the database untouched.
- **WSL**: cron is off by default — `sudo service cron start`, then `sudo systemctl enable cron`.
- Windows: use Task Scheduler instead — `schtasks /sc hourly /tn "Team5EventsSync" /tr "C:\path\to\python manage.py sync_events"`.

## Checks

The badge above shows the combined status of the Black, Flake8, and coverage jobs
in the pull request workflow. Click it to see workflow runs and individual job
results.

Start PostgreSQL with `docker compose up -d --wait db` before running tests.
The database user needs permission to create Django's temporary test database;
the Compose and CI users already have it. CI runs tests against PostgreSQL 17.

Run these before opening a pull request:

```bash
black --check .
flake8
coverage run manage.py test
coverage report  # fails below 95% coverage
```
