# Team Project repo

[![Checks](https://github.com/gcivil-nyu-org/team-5-fall26/actions/workflows/checks.yml/badge.svg?branch=develop)](https://github.com/gcivil-nyu-org/team-5-fall26/actions/workflows/checks.yml?query=branch%3Adevelop)

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

### Events without a photo

Cards and event pages show an illustration for the event's category when it
has no photo, or when its photo fails to load. The images are in
`events/static/events/categories/`, one per filter category, named after the
category's slug (for example `kids.webp`). Swimming and theater have their
own images (`CATEGORY_IMAGE_NAMES` in `events/models.py`), Seniors has none
because it describes an audience rather than an activity, and `park.webp`
covers events with no illustrated category. The first listed category with
an image decides the picture, except that Fitness only wins when nothing more
specific, such as yoga or dance, is listed. A test fails if a category points
at a missing image.

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

### Plain-language search

The search box on the home and search pages also understands plain language,
such as *kids events in the Bronx this weekend* or *fitness in Brooklyn on
Saturday morning*. What it recognizes becomes filters:

- **Dates:** today, tonight, tomorrow, this or next weekend, week, or month,
  weekdays, *in March*, and dates such as *Nov 20* or *11/20*.
- **Times of day:** morning, afternoon, evening, night, or clock times.
- **Boroughs.**
- **Audiences and broad kinds of event:** kids and families, seniors, fitness,
  sports, arts, music, nature, history, and education.

Filler such as *show me*, *things to do*, or *free*, and generic words such as
*classes*, are dropped, and the remaining words are the search terms. The page
then redirects to that filtered search, so the filter bar shows the filters
and they can be changed. A summary above the results shows how the search was
read, with a link to search for the exact words instead. When no event matches
every remaining word, events matching any of them are shown. Quoted phrases,
`OR`, and exclusions are kept as typed.

When `GEMINI_API_KEY` is set, searches of three or more words typed into
the search box are read by Gemini, so other phrasings and languages work too
(*actividades de naturaleza en Staten Island este fin de semana*). If Gemini
is unavailable, the built-in rules in `events/natural_search.py` are used.
Shared links with an explicit date expression, such as
`/search?q=kids+event+this+week`, are also understood using the built-in
rules. Other direct `/search?q=...` links, pagination, and changing only the
filters stay exact keyword searches. The "Search for these exact words
instead" link adds `exact=1` to bypass interpretation. The archive search is
always a keyword search.

## Event filters

A filter bar under the search box narrows results by date, time of day,
borough, and category, with or without a search term. Different filters must
all match; choosing several boroughs or categories matches any of them. Dates
and times use New York time, and an event matches a date range when it
overlaps it. **Time of day** keeps events that start in the morning (before
noon), afternoon (noon to 5 PM), or evening (5 PM or later). The
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

## Reporting events

Every event page has a **Report event** button. Signed-in users can report
hosted or imported events, including ended and cancelled events, without
being signed up to attend. Choose a reason and optionally add up to 5,000
characters of details, then submit to return to the event with a confirmation.

Each user can have one unresolved report per event. Submitting again keeps
the original report; after an admin resolves it, a new report can be submitted.
Reports retain their reason, details, timestamp, and the event title at the
time of submission even if the event or reporting account is deleted.
Reporting does not change the event or notify its host. The resolution
interface and admin report feed are future features.

Public comments and replies have a three-dot menu with **Report**. The report
form identifies the comment and its author, uses the same reasons and optional
details as event reporting, and returns to the event after submission. Users
cannot report their own comments. Reporting remains available on ended and
cancelled events. Each comment or reply can have one unresolved report per
reporter, independently of event reports or reports about its author.

The three-dot menu in a private conversation includes **Report user**. Only
participants with an accepted contact request can use it. A report targets the
other participant and stores references to the conversation and the latest
message at the time the report is submitted. The latest message can be from
either participant; an empty conversation has no message anchor. One unresolved
user report per reporter and reported user is allowed, even if newer messages
arrive afterward. Resolving it allows a new report with fresh context.

Shared community profiles have a three-dot menu with **Report profile** for
concerns about their photo, bio, or other public content. Any signed-in viewer
can report another member's shared profile, without a shared event or accepted
contact. Hidden profiles and your own profile cannot be reported this way.
The form uses the same reasons and optional details and returns to the profile
with confirmation. Each reporter can have one unresolved profile report per
member, independently of chat-user reports about that member.

All report types share `reports.Report` and its `kind` field. Comment reports
link through `reported_post` or `reported_reply`, `reported_user`, and the
parent `event`. Chat reports use `reported_user`, `conversation`, and
`context_message`; they do not copy chat content or identity snapshots.
Profile reports use `kind="profile"` and `reported_user`, leaving conversation,
message, event, and comment references empty. They link to the current profile
without copying its text or photo, so later edits change what an admin sees.
Source deletion clears the corresponding foreign keys and retains the report's
reason, details, and timestamps, so deleted chat history will not be available
for review. Admin viewing and navigation to the saved message are deferred to
the admin-reporting feature.

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

A chat widget (floating button, bottom-right of every page) answers questions
about events, bookings, support topics, and the signed-in visitor's own
account. It is powered by Google Gemini and only appears when
`GEMINI_API_KEY` is set. The key stays on the server: typed chat goes
through this site's own endpoints, and voice calls use a short-lived token
the server creates (see below).

**How it answers**

- **`POST /api/chat`** takes JSON `{"message": "...", "page": "/events/12/"}`
  and returns `{"reply", "escalated", "declined"}`, or `{"error"}` with a 4xx
  or 5xx status that the widget shows as a friendly message. The server
  builds the Gemini request from the system instructions, the FAQ, the site
  guide, the last 20 messages of the session's conversation, and the
  visitor's message.
- **Event data** comes from read-only database tools that Gemini calls as
  needed: event search (the same full-text search and filters as the search
  page), event details (date, time, venue, status, registration link,
  attendance counts), and the signed-in visitor's own activity. "Bookings"
  are the events a visitor chose **Want to Attend** or **Interested** on, so
  "When is my next event?" works. Tools only ever return the current
  visitor's own data. The site stores no prices or capacities, so the
  assistant only quotes a price when the event description gives one.
- **FAQ content** (booking, cancellations and refunds, payments, account and
  login help, events and venues) lives in the **FAQ entries** table. Edit,
  add, or deactivate entries in the Django admin; changes apply to the next
  message without a deploy. The first migration seeds 16 entries.
- **When it isn't sure** it says so and never promises that a person will
  follow up. For a question about an event, it suggests asking in the
  comments on that event's page, where the host and other attendees can
  answer. For anything else, it tells the visitor to contact the site admin
  (at `CHATBOT_SUPPORT_EMAIL` when it is set). These replies are logged with
  the outcome **Couldn't answer**, so staff can see which questions need FAQ
  entries.
- **Questions page.** Earlier versions forwarded unanswered questions to
  event managers. Those questions can still be answered on the **Questions**
  page (`/assistant/inquiries/`, linked from **My events**) or in the admin;
  the answer is translated into the asker's language and appears in their
  chat. New questions are no longer forwarded.
- **Off-topic or inappropriate requests** are declined by the system prompt
  and by Gemini's safety settings (harassment, hate speech, sexual, and
  dangerous content blocked at medium and above).
- **Any language.** Replies use the language of each message.
- **Voice conversations.** Tap the mic to start a live call with the
  assistant, a female US-English voice (Gemini Live API). She waits for you to
  speak first. Talk naturally: she replies out loud, can be interrupted, and both sides are transcribed
  into the chat as they speak. Typing during a call adds to it. Tap **End
  call** to hang up. How it works: `POST /api/chat/live` creates a
  single-use Gemini token locked to the assistant's voice, instructions
  (including recent chat history), and tools, and the browser streams audio
  to Gemini over a WebSocket with it. Tool calls run on the server
  (`POST /api/chat/live/tool`), and each finished exchange is saved to the
  conversation and the chat log (`POST /api/chat/live/turn`). A call lasts
  up to 30 minutes. Voice needs HTTPS (or `localhost`) and a current
  browser. Settings: `GEMINI_LIVE_MODEL` (default `gemini-3.8-live`),
  `GEMINI_LIVE_VOICE` (default `Achernar`, a soft voice; `Vindemiatrix`,
  `Sulafat`, `Aoede`, and `Kore` are other female voices), and `GEMINI_LIVE_LANGUAGE` (default `en-US`).
- **Limits and errors.** Each account or browser session can send 15
  messages or start 15 calls a minute (60 per network address); tool calls
  and saved turns during a call allow 60 a minute (240 per address). Gemini outages or quota errors
  show a friendly message instead of breaking the widget. Requests to Gemini
  time out after 20 seconds; replies normally take a few seconds.
- **Conversation logs.** Each exchange is logged anonymously (no user is
  stored; email addresses and phone or card numbers are masked) under **Chat
  logs** in the admin, with its outcome and response time, so staff can see
  which questions need new FAQ entries. Set
  `CHATBOT_LOG_CONVERSATIONS=False` to turn logging off.

### Set up the Gemini API key

1. Create an API key in [Google AI Studio](https://aistudio.google.com/apikey).
2. Locally, set `GEMINI_API_KEY` in `.env`. Never commit the key.
3. On Elastic Beanstalk, open the `team5-develop` environment, go to
   **Configuration → Updates, monitoring, and logging → Environment
   properties**, add `GEMINI_API_KEY`, and apply. The app restarts with the
   widget enabled.
4. Optional settings (same places):
   - `GEMINI_MODEL`: defaults to `gemini-flash-latest`, Google's alias for
     its newest Flash model. Pin a specific model for fully repeatable
     behaviour.
   - `CHATBOT_SUPPORT_EMAIL`: support address offered as a fallback
     (defaults to the first manager email).
   - `CHATBOT_MANAGER_EMAILS`: comma-separated staff emails told about
     forwarded questions.
   - `CHATBOT_LOG_CONVERSATIONS`: `False` to disable conversation logs.
5. Run `python manage.py migrate` (Elastic Beanstalk runs it on deploy) to
   create the FAQ, log, and questions tables.

Tests mock Gemini, so CI needs no key. Run `python manage.py test chatbot`
for the focused tests.

## Hourly sync

**On Elastic Beanstalk the sync runs automatically every hour.** After each
deployment, the postdeploy hook `.platform/sync/install_sync_timer.sh`
installs a systemd timer (`team5-sync.timer`). Amazon Linux 2023 has no cron,
and systemd timers are its built-in scheduler.

- It runs `manage.py sync_events` at :00 every hour, as the `webapp` user, with
  the same environment variables as the site.
- If the instance was down at the top of the hour, the missed run starts as
  soon as it comes back.
- A new run never starts while the previous one is still going.
- Each run appends its summary to `/var/log/team5_sync.log`. `eb logs`
  includes that file.
- On failure (feed down, bad payload), the sync exits non-zero and leaves the
  database untouched. The next hour tries again.
- If the timer can't be installed, the hook says why in
  `/var/log/eb-hooks.log`, and the deployment still succeeds.
- The environment is single-instance (`--single`). If it is ever scaled out,
  every instance runs the sync. That is safe because each run is one
  transaction, but wasteful.

To check it on the instance (`eb ssh`):

```bash
systemctl list-timers team5-sync.timer      # next and last run
systemctl status team5-sync.service         # result of the last run
sudo systemctl start team5-sync.service     # run a sync now
tail /var/log/team5_sync.log
```

**Locally** the sync is not scheduled. Run `python manage.py sync_events` when
you want fresh data, or register it with cron once per machine:

```bash
crontab -e
# add this line (absolute paths only; cron has no repo dir and no venv on PATH):
0 * * * * cd /path/to/team-5-fall26 && /path/to/.venv/bin/python manage.py sync_events >> /tmp/team5_sync.log 2>&1
```

- **WSL**: cron is off by default — `sudo service cron start`, then `sudo systemctl enable cron`.
- Windows: use Task Scheduler instead — `schtasks /sc hourly /tn "Team5EventsSync" /tr "C:\path\to\python manage.py sync_events"`.

## Checks

The Checks workflow runs Black, Flake8, and tests with a 95% coverage minimum on
pull requests, pushes to `main` and `develop`, and manual runs. It uses Python
3.11. Tests and the coverage minimum run in forks without deployment credentials
or Coveralls setup. Click the badge above to see workflow runs and individual
job results.

To upload successful coverage results to Coveralls, set the repository variable
`COVERALLS_ENABLED` to `true` under **Settings → Secrets and variables → Actions
→ Variables**. Uploads run only on pushes, using GitHub's automatic
`GITHUB_TOKEN`; no Coveralls secret is required. Pull requests and manual runs
still enforce the coverage minimum without uploading. Unset or `false` disables
uploads without affecting tests.

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

With the repository variable `DEPLOY_ENABLED` set to `true`, GitHub Actions
deploys pushes to `develop` to the configured Elastic Beanstalk environment
(default: `team5-develop`) after Black, Flake8, and coverage pass. With the
variable unset or `false`, deployment is skipped. Pull requests, `main`, and
manual workflow runs only run checks. Deployments use the tested commit, include
the Elastic Beanstalk configuration and platform hooks, and wait for the
environment update before verifying the deployed version. Deployments run one
at a time; newer pushes do not cancel an active deployment.

Both opt-in variables are repository configuration, rather than workflow `env`
values or secrets. Forks must configure their own variables to enable these
integrations. The conditions do not depend on the repository name or owner.

Before the first Actions deployment:

1. In the GitHub repository, open **Settings → Secrets and variables → Actions**
   and add repository secrets `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`
   using the deployment credentials previously configured in Travis. Travis
   secrets are not transferred automatically. These names match this repo's old
   Travis configuration.
2. Ensure that IAM principal can upload source bundles to
   `elasticbeanstalk-us-east-1-390844769109`, create application versions for
   `team5`, update `team5-develop`, and describe the environment in `us-east-1`.
   These are the default deployment targets. To deploy to a different environment
   (including from a fork), set the repository variables below and configure
   credentials for that target before enabling deployment.
3. Enable GitHub Actions for the repository and allow the actions referenced by
   the workflow, including the [Coveralls action](https://github.com/coverallsapp/github-action).
4. Disable builds for this repository in Travis to stop duplicate builds. If
   branch protection requires Travis, replace that required check with the
   Actions **Black**, **Flake8**, and **Coverage** checks after their first run.
5. Under **Settings → Secrets and variables → Actions → Variables**, add
   `DEPLOY_ENABLED` with the value `true`. Set `COVERALLS_ENABLED=true` separately
   if coverage uploads are wanted. These flags enable integrations on subsequent
   pushes; changing a variable does not trigger a workflow run.

Optional deployment repository variables (defaults preserve the existing target):

| Variable | Default |
| --- | --- |
| `EB_REGION` | `us-east-1` |
| `EB_APPLICATION` | `team5` |
| `EB_ENVIRONMENT` | `team5-develop` |
| `EB_BUCKET` | `elasticbeanstalk-us-east-1-390844769109` |

The repo is configured for Elastic Beanstalk's Python platform:
`.ebextensions/django.config` sets `DJANGO_SETTINGS_MODULE`, the WSGI path
(`config.wsgi:application`), and the `/static` mapping, and runs `migrate`,
`collectstatic`, and an initial `sync_events` on each deploy; a postdeploy
hook then schedules the [hourly sync](#hourly-sync). Settings read the
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

### HTTPS

Browsers only allow the microphone, and so voice chat, on HTTPS pages. The
single instance serves HTTPS itself, with a free Let's Encrypt certificate for
the environment's own address, so no load balancer or domain is needed.

- **Configuration:** `.ebextensions/https.config` opens port 443 and sets
  `HTTPS_DOMAIN`, the address the certificate is for. Change it for another
  environment, or set it to an empty value to serve plain HTTP only.
- **After each deployment:** the postdeploy hooks run
  `.platform/https/enable_https.sh`. It installs certbot, gets or renews the
  certificate, serves the site on port 443, and redirects HTTP to HTTPS.
- **Logs and health:** HTTPS requests go to the same nginx access log and
  enhanced health log as HTTP ones, so health metrics keep working after the
  redirect.
- **Certificate checks:** Let's Encrypt fetches files from
  `/.well-known/acme-challenge/` over plain HTTP, set up in
  `.platform/nginx/conf.d/elasticbeanstalk/acme_challenge.conf`.
- **Renewal:** a systemd timer runs certbot twice a day, and certbot only
  renews when the certificate is close to expiring.
- **Failures:** if a step fails, the script logs why in
  `/var/log/eb-hooks.log` and the site stays on plain HTTP; deployments never
  fail because of HTTPS. If renewing fails while the current certificate is
  still valid, HTTPS keeps using it. A replaced instance gets a new
  certificate on its first deployment.
- **Django:** nginx tells Django that requests arrived over HTTPS, which
  `SECURE_PROXY_SSL_HEADER` already trusts in production.
- **Google sign-in:** add
  `https://<HTTPS_DOMAIN>/accounts/google/login/callback/` to the Google OAuth
  client's authorized redirect URIs, since sign-in now returns over HTTPS.

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


## Discover people interested in similar events (#54)

Event pages can suggest members who have marked **other upcoming events** as
Interested when the public categories match or belong to the same existing
category group. The section only shows public profile links and a general
category label, not a user's email or the title of their other event.

Discovery is **off by default**. Members must enable both profile sharing and
the separate similar-event discovery setting in Account Settings. Recommendations
also require both people to have a currently selected matching topic under
**My Interests**, relevant to the event categories. Deselecting a shared topic
removes the recommendation on the next page load, and reselecting it can restore
the match. The separate direct-contact preference does not control discovery.
Opting out of profile sharing or discovery also hides a member immediately.
Banned or inactive members, past/cancelled events, and members already on the
current event's Interested or registration lists are not suggested. Signed-out and banned viewers cannot browse these
suggestions. Public profiles only show a related-interest explanation when the
viewer actually follows the referenced event and the owner is still opted in.

This PR is only about **finding people**. Changes to contact requests and
messaging will be reviewed separately.
