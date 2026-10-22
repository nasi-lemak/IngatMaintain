# IngatMaintain

*Ingat* (Malay: "remember"). A small, self-hosted web app that reminds the team
when maintenance is due on clients' on-prem (offline) systems. It also gives the
team a checklist to cross off on site.

Example: the MQTT (Mosquitto) SSL certificates on the 5 computers at
**NHG Khatib Polyclinic** expire on **21 Oct 2028**. IngatMaintain:

- reminds the team 180, 120, 90, 60, 30, 14, 7 and 1 days before, then weekly once overdue
  (by email, Telegram or a Slack/Teams/Google Chat webhook, and always on the dashboard).
  Reminders start early because Synapxe has to sign the CSRs.
- shows the packing list before the visit:
  - **software needed on our laptop**, e.g. OpenSSL
  - **items to bring**, e.g. the Lenovo secure drive, a portable monitor and a portable keyboard
- gives a step-by-step checklist with **one tick box per computer** for per-machine steps:
  generate the CSR, copy it to the secure drive, install the .cer, export the key, run OpenSSL,
  copy the files into the Mosquitto path, restart and verify.
- records who ticked each box and when, and keeps a notes log
  (e.g. "CSRs sent to Synapxe, ticket #123").
- asks for the new expiry date when the job is completed and restarts the reminders from it.

## Concepts

| Thing | What it is |
|---|---|
| **Client** | A site, its contact and access notes, and the **computers** installed there. |
| **Checklist** | A reusable procedure with ordered steps (grouped into sections, each ticked *once* or *for each computer*), the laptop software it needs and the items to bring. |
| **Maintenance item** | Something at a client that expires or recurs (SSL certificate, licence, patching…). Has a due/expiry date, reminder days and a default checklist. |
| **Software / versions** | Products you deploy to sites (your own apps, Mosquitto…), their releases, and which version is on each client computer. |
| **Job** | One run of a checklist for an item, covering one or more visits. Starting a job copies the checklist, so later edits to the template don't change jobs already in progress. |

A job's page works well on a phone, and every tick saves straight away. If the site
has no signal (a server room, for example), use **Print checklist** for a paper
copy, then tick the boxes online afterwards.

### Software versions

Under **Versions**, add each product you deploy and its releases. The highest version
number counts as the latest. Pre-releases such as `2.4.0-rc1` sort before `2.4.0`.

To record what's installed, open **Record versions** from a client's page, or from
the job page after a deployment visit. That opens a grid of computers × software:
- Type the version installed on each computer.
- To set the same version on every computer at once, fill in **All computers** for that column.
- Clearing a box means the software was removed.

Only real changes are saved. Each one goes into the version history with the date,
who recorded it, and the job it was done on.

When you add a new release, every site still on an older version shows up under
**Sites behind on software** on the dashboard and the Versions page. Its **Plan upgrade**
button creates a maintenance item ("Upgrade Clinic Queue App to 2.4.0"). From there,
start a job with a deployment checklist as usual.

### Visits and rescheduling

A job starts with one site visit. You can change its date or team at any time from the
**Visits** card on the job page. Each change is added to the job log ("Visit moved from
4 May 2028 to 11 May 2028"), with an optional reason. The calendar feed moves the event as well.

If the work can't be finished in one trip, use **+ Add another visit**. For example,
Synapxe may not return the .cer files on the same day. Each visit has its own date and
team and can be rescheduled separately. The checklist carries over between visits, so
nothing needs re-ticking.

The day before each visit, the team gets a reminder with the date, site address, access
notes, who's going, the items not yet packed and the laptop software not yet checked.
If a visit is rescheduled, a new reminder goes out for the new date.

### Logins and share links

Everyone on the team has their own login, so each tick records who did it. A
device stays logged in for a year after its last visit (`SESSION_LIFETIME_DAYS`),
so people rarely see the login page.

To let someone without an account help, such as a colleague or a contractor, open
the job and click **Create share link**. You can send the link straight to WhatsApp
from there. The person opens it on their phone, enters their name, and can then
tick that job's checklist and add notes. Their ticks show as "Sam Lee (shared link)".

The link shows only that job: its checklist, packing list, site address and access
notes, and computer names. It shows no other clients, no hostnames or IPs, and no
settings. It stops working when any of these happens:
- the job is completed or cancelled
- someone clicks **Turn off link**
- someone renews it (the old link stops and a new one is made)
- after `SHARE_LINK_DAYS` (default 30)

## Running it

### With Docker (recommended)

```bash
cp .env.example .env        # set SECRET_KEY, BASE_URL and a notification channel
docker compose up -d --build
```

Open http://localhost:8000. The first visit asks you to create the admin account.
Data is stored in the `ingatmaintain-data` volume (one SQLite file).

### Without Docker

Needs Python 3.10 or newer. Works on Linux, macOS and Windows.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python run.py                      # serves on http://0.0.0.0:8000
```

`run.py` also checks reminders once at start-up and then every day at `REMINDER_HOUR`.
To use system cron or Task Scheduler instead, set `REMINDER_SCHEDULER=false` and run:

```bash
flask --app ingatmaintain send-reminders            # add --dry-run to preview
```

It's safe to run this any number of times. Each reminder is sent once, and a failed send is retried on the next run.

### Other commands

```bash
flask --app ingatmaintain create-user alice --name "Alice Tan" --email alice@example.com [--admin]
flask --app ingatmaintain seed-example   # loads the NHG Khatib Polyclinic example
```

Admins can also load the example from **Settings**, or from the dashboard while it is empty.

## Configuration

All settings are environment variables. See `.env.example` for the full list.

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Session signing key. If unset, one is generated and stored next to the database. |
| `BASE_URL` | Public URL, used in reminder links and the calendar feed. |
| `APP_TIMEZONE` | Default `Asia/Singapore`. |
| `REMINDER_HOUR` | Hour of day for the daily check (default 8). |
| `DATABASE_PATH` | SQLite file location (default `instance/ingatmaintain.sqlite3`). |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_STARTTLS`, `SMTP_SSL` | Email reminders. Sent to every active user who has an email address and has reminders turned on. |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Post reminders to a Telegram group. |
| `WEBHOOK_URL` | Slack / Teams / Google Chat incoming webhook. The JSON payload has a `text` field. |
| `SESSION_COOKIE_SECURE` | Set to `true` when served over HTTPS. |
| `SESSION_LIFETIME_DAYS` | How long a device stays logged in after its last visit (default 365). |
| `SHARE_LINK_DAYS` | How long a job's share link works at most (default 30). |

**Settings → Send test notification** checks that the channels work.
**Settings → Calendar feed** gives a private `.ics` link. Subscribe to it in Google Calendar
or Outlook to see every expiry date and planned site visit.

## How reminders work

- Each item has a list of "days before" marks (default `180,90,60,30,14,7,1`).
  When a mark is reached, one reminder goes out, as a daily digest that covers all items.
- After the due date, a reminder goes out every week until the job is completed or the item is archived.
- Site visits are reminded the day before (or on the day, if booked late), in the same daily message.
- Completing a job sets the new due date, so the reminder cycle starts again for that date.
- If no channel is configured or every channel fails, nothing is marked as sent, and the
  reminders go out on the next run once the problem is fixed. The dashboard always shows what's due.

Dashboard colours: **overdue** (past the due date), **due ≤ 30 days**, **plan now**
(inside the first reminder window) and **OK**.

## Development

```bash
pip install -r requirements-dev.txt
pytest
FLASK_DEBUG=1 flask --app ingatmaintain run      # auto-reload dev server on :5000
```

Code layout: `ingatmaintain/views/*` (one blueprint per area), `reminders.py` (the reminder engine),
`schema.sql` (all tables), `seed.py` (example data).

Security notes: logins use hashed passwords, and every form is protected against CSRF.
Don't store client passwords or private keys in the app. Keep those in your password manager.
