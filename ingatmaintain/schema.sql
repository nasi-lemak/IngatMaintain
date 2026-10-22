-- IngatMaintain schema. Every statement is idempotent so it can run on each start.

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    full_name     TEXT NOT NULL DEFAULT '',
    email         TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    notify        INTEGER NOT NULL DEFAULT 1,   -- receives reminder emails
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clients (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE COLLATE NOCASE,
    site_address  TEXT NOT NULL DEFAULT '',
    contact_name  TEXT NOT NULL DEFAULT '',
    contact_phone TEXT NOT NULL DEFAULT '',
    contact_email TEXT NOT NULL DEFAULT '',
    access_notes  TEXT NOT NULL DEFAULT '',   -- how to get on site: registration, escort, hours
    notes         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Computers / devices installed at a client site.
CREATE TABLE IF NOT EXISTS machines (
    id         INTEGER PRIMARY KEY,
    client_id  INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    hostname   TEXT NOT NULL DEFAULT '',
    ip_address TEXT NOT NULL DEFAULT '',
    location   TEXT NOT NULL DEFAULT '',
    os         TEXT NOT NULL DEFAULT '',
    notes      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_machines_client ON machines(client_id);

-- Reusable checklist templates ("procedures").
CREATE TABLE IF NOT EXISTS procedures (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS procedure_steps (
    id           INTEGER PRIMARY KEY,
    procedure_id INTEGER NOT NULL REFERENCES procedures(id) ON DELETE CASCADE,
    position     INTEGER NOT NULL,
    section      TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL,
    details      TEXT NOT NULL DEFAULT '',
    per_machine  INTEGER NOT NULL DEFAULT 0  -- 1 = tick once for every computer
);
CREATE INDEX IF NOT EXISTS idx_procedure_steps ON procedure_steps(procedure_id, position);

-- kind: 'software' (needed on our laptop) or 'equipment' (items to bring).
CREATE TABLE IF NOT EXISTS procedure_requirements (
    id           INTEGER PRIMARY KEY,
    procedure_id INTEGER NOT NULL REFERENCES procedures(id) ON DELETE CASCADE,
    position     INTEGER NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('software', 'equipment')),
    name         TEXT NOT NULL,
    quantity     TEXT NOT NULL DEFAULT '',
    details      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_procedure_reqs ON procedure_requirements(procedure_id, position);

-- Something at a client that expires or recurs (SSL cert, licence, patching...).
CREATE TABLE IF NOT EXISTS maintenance_items (
    id                INTEGER PRIMARY KEY,
    client_id         INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    procedure_id      INTEGER REFERENCES procedures(id) ON DELETE SET NULL,
    title             TEXT NOT NULL,
    category          TEXT NOT NULL DEFAULT '',
    due_date          TEXT,                 -- YYYY-MM-DD (expiry / next due)
    reminder_days     TEXT NOT NULL,        -- e.g. "180,90,60,30,14,7,1"
    recurrence_months INTEGER,              -- suggested interval when rolling over
    notes             TEXT NOT NULL DEFAULT '',
    archived          INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_items_client ON maintenance_items(client_id);
CREATE INDEX IF NOT EXISTS idx_items_due ON maintenance_items(due_date);

-- A job is one run of a checklist for a maintenance item. Steps, requirements
-- and machines are copied in when the job starts, so later template edits
-- never change a job that is already under way.
CREATE TABLE IF NOT EXISTS jobs (
    id                INTEGER PRIMARY KEY,
    item_id           INTEGER NOT NULL REFERENCES maintenance_items(id) ON DELETE CASCADE,
    title             TEXT NOT NULL,
    procedure_name    TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'completed', 'cancelled')),
    scheduled_date    TEXT,
    assigned_to       TEXT NOT NULL DEFAULT '',
    created_by        TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    completed_at      TEXT,
    completed_by      TEXT,
    previous_due_date TEXT,
    new_due_date      TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_item ON jobs(item_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS job_machines (
    id         INTEGER PRIMARY KEY,
    job_id     INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    machine_id INTEGER REFERENCES machines(id) ON DELETE SET NULL,
    position   INTEGER NOT NULL,
    name       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_steps (
    id          INTEGER PRIMARY KEY,
    job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    section     TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL,
    details     TEXT NOT NULL DEFAULT '',
    per_machine INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS job_requirements (
    id       INTEGER PRIMARY KEY,
    job_id   INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    kind     TEXT NOT NULL CHECK (kind IN ('software', 'equipment')),
    name     TEXT NOT NULL,
    quantity TEXT NOT NULL DEFAULT '',
    details  TEXT NOT NULL DEFAULT ''
);

-- One row per ticked box. machine_id is a job_machines.id, or 0 for "whole job".
CREATE TABLE IF NOT EXISTS job_checks (
    id         INTEGER PRIMARY KEY,
    job_id     INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL CHECK (kind IN ('step', 'req')),
    ref_id     INTEGER NOT NULL,
    machine_id INTEGER NOT NULL DEFAULT 0,
    checked_by TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    UNIQUE (kind, ref_id, machine_id)
);
CREATE INDEX IF NOT EXISTS idx_job_checks_job ON job_checks(job_id);

-- Timestamped log for a job ("CSRs sent to Synapxe, ref #1234").
CREATE TABLE IF NOT EXISTS job_notes (
    id         INTEGER PRIMARY KEY,
    job_id     INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    author     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    body       TEXT NOT NULL
);

-- Which reminders have gone out. threshold > 0: "N days before" bucket;
-- threshold < 0: weekly overdue bucket. Keyed by due_date so rolling the item
-- over to a new date starts a fresh reminder cycle.
CREATE TABLE IF NOT EXISTS reminder_log (
    id        INTEGER PRIMARY KEY,
    item_id   INTEGER NOT NULL REFERENCES maintenance_items(id) ON DELETE CASCADE,
    due_date  TEXT NOT NULL,
    threshold INTEGER NOT NULL,
    sent_at   TEXT NOT NULL,
    channels  TEXT NOT NULL DEFAULT '',
    UNIQUE (item_id, due_date, threshold)
);
