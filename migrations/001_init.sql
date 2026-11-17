-- Every inbound payload, verbatim. The idempotency key makes webhook retries harmless.
CREATE TABLE raw_leads (
    id              bigserial PRIMARY KEY,
    source          text NOT NULL,
    idempotency_key text NOT NULL UNIQUE,
    payload         jsonb NOT NULL,
    received_at     timestamptz NOT NULL DEFAULT now(),
    contact_id      bigint
);

-- One row per real-world company, after dedupe.
CREATE TABLE companies (
    id             bigserial PRIMARY KEY,
    canonical_name text,
    name_key       text,                         -- normalized name used for fuzzy matching
    country        char(2),
    employees      int,
    industry       text,
    description    text,
    signals        jsonb NOT NULL DEFAULT '[]',
    provenance     jsonb NOT NULL DEFAULT '{}',  -- field -> {lead, at}: which lead set it, when
    owner          text,
    route          text,
    alerted_at     timestamptz,                  -- AE alert sent; set once, never cleared
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX companies_country_name ON companies (country, name_key);

-- A company can own many domains (acme.de, acme.com).
CREATE TABLE company_domains (
    domain     text PRIMARY KEY,
    company_id bigint NOT NULL REFERENCES companies(id)
);

CREATE TABLE contacts (
    id         bigserial PRIMARY KEY,
    email      text UNIQUE,                      -- NULL allowed: lead without an email
    name       text,
    title      text,
    seniority  text,
    department text,
    company_id bigint REFERENCES companies(id),  -- NULL = unassigned
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX contacts_company ON contacts (company_id);

CREATE TABLE scores (
    company_id    bigint PRIMARY KEY REFERENCES companies(id),
    total         int NOT NULL,
    tier          char(1) NOT NULL CHECK (tier IN ('A', 'B', 'C')),
    breakdown     jsonb NOT NULL,
    model_version text NOT NULL,
    scored_at     timestamptz NOT NULL DEFAULT now()
);

-- The job queue. Workers claim rows with FOR UPDATE SKIP LOCKED.
CREATE TABLE jobs (
    id           bigserial PRIMARY KEY,
    kind         text NOT NULL,
    payload      jsonb NOT NULL,
    status       text NOT NULL DEFAULT 'queued'
                 CHECK (status IN ('queued', 'running', 'done', 'dead')),
    attempts     int NOT NULL DEFAULT 0,
    max_attempts int NOT NULL DEFAULT 3,
    run_after    timestamptz NOT NULL DEFAULT now(),
    last_error   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX jobs_ready ON jobs (run_after) WHERE status = 'queued';

CREATE TABLE crm_sync (
    company_id       bigint PRIMARY KEY REFERENCES companies(id),
    external_id      text,
    last_synced_hash text,
    synced_at        timestamptz NOT NULL DEFAULT now()
);

-- Offline stand-in for HubSpot.
CREATE TABLE fake_crm (
    object      text NOT NULL,
    key         text NOT NULL,
    external_id text NOT NULL,
    props       jsonb NOT NULL,
    PRIMARY KEY (object, key)
);

CREATE TABLE classify_cache (
    provider     text NOT NULL,
    content_hash text NOT NULL,
    result       jsonb NOT NULL,
    latency_ms   int,
    PRIMARY KEY (provider, content_hash)
);

CREATE TABLE briefs (
    id          bigserial PRIMARY KEY,
    company_id  bigint NOT NULL REFERENCES companies(id),
    brief       text NOT NULL,
    email_draft text NOT NULL,
    status      text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    approved_at timestamptz
);
