# Team Project repo

[![Checks](https://github.com/gcivil-nyu-org/team-5-fall26/actions/workflows/checks.yml/badge.svg?branch=develop)](https://github.com/gcivil-nyu-org/team-5-fall26/actions/workflows/checks.yml?query=branch%3Adevelop)

[![Build Status](https://app.travis-ci.com/gcivil-nyu-org/team-5-fall26.svg?branch=develop)](https://app.travis-ci.com/github/gcivil-nyu-org/team-5-fall26)

[![Coverage Status](https://coveralls.io/repos/github/gcivil-nyu-org/team-5-fall26/badge.svg?branch=develop)](https://coveralls.io/github/gcivil-nyu-org/team-5-fall26?branch=develop)


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

Hosts can also **Cancel event** from the detail page, at `/events/<id>/cancel/`.
A confirmation page lets them add an optional message, and cancelling can't
be undone. A cancelled event leaves the home page, search, and filters, and
its page shows a cancellation banner with interest, comments, and editing
closed. Everyone who selected Interested or Want to Attend gets an email and
a notification. **My events** (`/events/mine/`), linked in the header, lists a
host's upcoming events and archives the cancelled and ended ones.

## Notifications

While an event is upcoming, its host can message everyone who selected
**Interested** or **Want to Attend** from the event's detail page. The host
sees how many people will get it, but not who they are. Each person gets an
email and an in-app notification. When a host changes an event's date, time, or location,
interested users are told automatically.

Signed-in users see a **Notifications** link on the home page with their
unread count. `/notifications/` lists contact requests first, followed by
event updates and discussion replies, with the newest items first within each
group. Event updates and reply alerts are marked read when displayed. Pending
contact requests remain unread until accepted or declined.

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

After a contact request is accepted, either attendee can open the other
person's profile and choose **Send Message**, or use the **Message** link on
the contact notification. The **Messages** page contains one conversation per pair;
the shared event is shown during the contact-request/acceptance flow rather
than repeated in the chat.
Chats update automatically while open (messages are checked every three
seconds), and new messages are shown as unread on the **Messages** page until
the recipient opens that conversation. Message traffic does not create
separate notifications. Conversations remain available after an event ends,
and private messages do not send email.

In development, event-update emails are printed to the `runserver` console
instead of being sent.

## Parks Assistant (chatbot)

A chat widget in the bottom-right corner of every page answers questions about
events, how the site works, and the signed-in visitor's own plans. It is
powered by Google Gemini and only appears when `GEMINI_API_KEY` is set.

- **Grounded in the database.** The assistant uses Gemini function calling to
  run read-only lookups: event search (the same full-text search and filters
  as the search page), event details, and the visitor's own activity (planned
  and hosted events, unread notifications and messages). It never reveals
  other members' details. On an event page it knows which event "this event"
  means.
- **Any language.** It replies in the language of each message. Search terms
  are translated to English behind the scenes.
- **Voice.** Tap the mic, speak in any language, and tap again to send (up to
  one minute). The browser records 16 kHz WAV audio; Gemini transcribes it,
  and the reply is read aloud in the same language. Every reply also has a
  **Listen** button. Voice input needs HTTPS (or `localhost`).
- **Unanswered questions go to the event manager.** When the answer isn't in
  the site data, the assistant says it will check with the event manager and
  saves the question. The host of a posted event gets a notification and
  email; questions about NYC Parks feed events (or no event) go to staff, who
  are emailed when `CHATBOT_MANAGER_EMAILS` is set. Managers answer on the
  **Questions** page at `/assistant/inquiries/` (linked from **My events**) or
  in the Django admin. The answer is translated into the asker's language and
  shown in their chat window; signed-in askers also get an email, plus a
  notification for event questions.
- The conversation (last 20 messages) is kept in the visitor's session and
  follows them between pages. Each visitor can send 15 messages a minute.

### Set up Gemini

1. Create an API key in [Google AI Studio](https://aistudio.google.com/apikey).
2. Set `GEMINI_API_KEY` in your local `.env`, or as an environment property in
   the Elastic Beanstalk console. Never commit the key.
3. Optionally set `GEMINI_MODEL` (default `gemini-flash-latest`, Google's alias
   for the newest Flash model) and `CHATBOT_MANAGER_EMAILS` (comma-separated).
4. Run `python manage.py migrate` to create the questions table.

Tests mock Gemini, so CI needs no key. Run
`python manage.py test chatbot` for the focused tests.

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

## Deploying to AWS Elastic Beanstalk

The repo is configured for Elastic Beanstalk's Python platform:
`.ebextensions/django.config` sets `DJANGO_SETTINGS_MODULE`, the WSGI path
(`config.wsgi:application`), and the `/static` mapping, and runs `migrate`,
`collectstatic`, and an initial `sync_events` on each deploy. Settings read the
`RDS_*` variables that Elastic Beanstalk provides when a database is attached,
and `*.elasticbeanstalk.com` is already in `ALLOWED_HOSTS`.

Install the [EB CLI](https://docs.aws.amazon.com/elasticbeanstalk/latest/dg/eb-cli3-install.html)
and configure AWS credentials for an IAM user (never the root account, and
never commit keys). Then, from the repo root on the `develop` branch:

```bash
eb init -p "python-3.11" team5 --region us-east-1
eb init                      # answer "yes" to set up SSH, pick or create a key pair
eb create team5-develop --single --instance_type t3.micro \
  --database --database.engine postgres --database.instance db.t3.micro \
  --database.username team5admin --database.password '<choose-a-password>' \
  --envvars DJANGO_SECRET_KEY='<random-secret>'
eb open
```

Deploy later commits with `eb deploy`. The EB CLI deploys the last commit, so
commit changes first. `eb terminate team5-develop` removes the environment
and its database. If instances report degraded health because there is no
default VPC, create one in the AWS VPC console (Actions → Create default VPC).
Uploaded profile photos are stored on the instance and are lost when it is
replaced.

## Google sign-in (issue #29)

Google sign-in is available alongside the existing username/email + password
login and registration. It uses django-allauth's Google OAuth 2.0 integration
with PKCE, state validation, and verified Google email addresses. First-time
Google users get a local account automatically; returning users can sign in
again with Google. A verified Google email matching an existing local account
signs in to and connects that account. Unverified addresses cannot be used
for this linking. Google tokens are not stored in the app database.

### Set up Google OAuth

1. In [Google Cloud Console](https://console.cloud.google.com/apis/credentials),
   configure the OAuth consent screen and create an **OAuth client ID** of
   type **Web application**. For external/testing apps, add the Google accounts
   you use as test users on the consent screen.
2. Add these exact **Authorized redirect URIs**:
   - `http://127.0.0.1:8000/accounts/google/login/callback/` (local)
   - `https://team5-develop.eba-e8eb3pzq.us-east-1.elasticbeanstalk.com/accounts/google/login/callback/` (deployed site)
   If you run locally with `localhost` instead, authorize its callback too.
3. Set `GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_CLIENT_SECRET` in
   your local `.env` (never commit this file). For Elastic Beanstalk, set
   these as environment properties in the EB console for `team5-develop`.
   Do not put credentials into `.ebextensions`, README, Git, or a PR.
4. Run `python manage.py migrate` locally. The deployment configuration
   already runs migrations on Elastic Beanstalk. Restart/redeploy the app
   after changing environment variables.

The Google buttons appear on both `/accounts/login/` and
`/accounts/register/` only when **both** credentials are configured.
Password-based registration and login still work without Google credentials.

For a deployed OAuth request, Google should see the HTTPS callback listed
above (Elastic Beanstalk supplies `X-Forwarded-Proto`). On the login page,
the Google button is a CSRF-protected POST; OAuth authorization is completed
through Google's hosted consent page, not by collecting Google passwords.

Local and CI tests don't need real Google credentials. Run
`python manage.py test accounts.tests.test_google_sso` for the focused tests.
