```sql
-- =====================================================================
-- TitanPort Database Schema
-- Supabase PostgreSQL
--
-- Run in:
-- Supabase Dashboard -> SQL Editor -> New query -> Run
--
-- Structure:
--   1. Enums
--   2. Tables
--   3. Indexes
--   4. Triggers / functions
--   5. Row Level Security
-- =====================================================================


-- =====================================================================
-- 1. ENUMS
-- =====================================================================

create type case_status as enum (
  'OPEN',
  'AWAITING_DOCUMENT',
  'AWAITING_REVIEW',
  'RESOLVED',
  'CLOSED'
);

create type case_resolution as enum (
  'MATCHED',
  'ACCEPTED_WITH_DISCREPANCIES',
  'CORRECTED',
  'OTHER'
);

create type activity_type as enum (
  'EMAIL_CLASSIFICATION',
  'DOCUMENT_IDENTIFICATION',
  'SI_EXTRACTION',
  'BL_EXTRACTION',
  'EXTRACTION_VALIDATION',
  'FIELD_MATCHING',
  'NORMALIZATION',
  'COMPARISON',
  'DISCREPANCY_REPORTING',
  'HUMAN_REVIEW',
  'DOCUMENT_CORRECTION'
);

create type activity_state as enum (
  'CREATED',
  'IN_PROGRESS',
  'COMPLETED',
  'RETRY_REQUIRED',
  'FAILED',
  'REQUIRES_REVIEW',
  'BLOCKED'
);

create type email_category as enum (
  'DOCUMENT_COMPARISON',
  'NEW_SI_REQUEST',
  'INVOICE_QUERY',
  'GENERAL',
  'SPAM'
);

create type case_relationship as enum (
  'NEW_CASE',
  'EXISTING_CASE',
  'UNKNOWN'
);

create type document_type as enum (
  'SI',
  'BL',
  'INVOICE',
  'OTHER',
  'UNKNOWN'
);

create type validation_result as enum (
  'VALID',
  'INVALID',
  'UNCERTAIN'
);

create type comparison_result as enum (
  'MATCH',
  'MISMATCH'
);

create type review_status as enum (
  'OPEN',
  'ASSIGNED',
  'RESOLVED',
  'CANCELLED'
);

create type decision_type as enum (
  'CORRECT_EXTRACTION',
  'MISSING_DOCUMENT',
  'ACCEPT_OVERRIDE',
  'AUTHORIZE_CORRECTION'
);

create type actor_type as enum (
  'AGENT',
  'SYSTEM',
  'HUMAN'
);


-- =====================================================================
-- 2. TABLES
-- =====================================================================


-- ---------------------------------------------------------------------
-- Human reviewers / case officers
-- ---------------------------------------------------------------------

create table case_officers (
  id                        uuid primary key default gen_random_uuid(),
  full_name                 text not null,
  email                     text unique not null,
  can_authorize_corrections boolean not null default false,
  is_active                 boolean not null default true,
  created_at                timestamptz not null default now()
);


-- ---------------------------------------------------------------------
-- Cases
-- A case represents one shipment / work item.
-- ---------------------------------------------------------------------

create sequence case_ref_seq start 1001;

create table cases (
  id                uuid primary key default gen_random_uuid(),
  case_ref          text unique not null
                    default ('SH-' || nextval('case_ref_seq')::text),
  shipment_ref      text,
  title             text,
  status            case_status not null default 'OPEN',
  resolution        case_resolution,
  resolution_notes  text,
  resolved_at       timestamptz,
  closed_at         timestamptz,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);


-- ---------------------------------------------------------------------
-- Emails
-- Emails are linked to cases after classification.
-- ---------------------------------------------------------------------

create table emails (
  id                        uuid primary key default gen_random_uuid(),
  case_id                   uuid references cases(id),
  gmail_message_id          text unique not null,
  gmail_thread_id           text,
  subject                   text,
  sender                    text,
  recipients                text[],
  body_text                 text,
  received_at               timestamptz,
  category                  email_category,
  case_relationship         case_relationship,
  classification_confidence numeric(4,3)
                            check (classification_confidence between 0 and 1),
  created_at                timestamptz not null default now()
);


-- ---------------------------------------------------------------------
-- Documents
-- Stores SI / BL documents and their versions.
-- ---------------------------------------------------------------------

create table documents (
  id               uuid primary key default gen_random_uuid(),
  case_id          uuid not null references cases(id),
  email_id         uuid references emails(id),
  doc_type         document_type not null default 'UNKNOWN',
  type_confidence  numeric(4,3)
                   check (type_confidence between 0 and 1),
  version          int not null default 1,
  supersedes_id    uuid references documents(id),
  is_current       boolean not null default true,
  file_name        text,
  mime_type        text,
  storage_path     text,
  language         text,
  created_by       actor_type not null default 'SYSTEM',
  created_at       timestamptz not null default now()
);


-- ---------------------------------------------------------------------
-- Activities
-- One row per unit of work within a case.
-- ---------------------------------------------------------------------

create table activities (
  id              uuid primary key default gen_random_uuid(),
  case_id         uuid not null references cases(id),
  document_id     uuid references documents(id),
  activity_type   activity_type not null,
  state           activity_state not null default 'CREATED',
  attempt_count   int not null default 0,
  max_retries     int not null default 2,
  blocked_reason  text,
  last_error      text,
  started_at      timestamptz,
  completed_at    timestamptz,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);


-- ---------------------------------------------------------------------
-- Extracted fields
-- Structured values extracted from SI / BL documents.
-- ---------------------------------------------------------------------

create table extracted_fields (
  id                  uuid primary key default gen_random_uuid(),
  document_id         uuid not null references documents(id),
  activity_id         uuid references activities(id),
  field_name          text not null check (
    field_name in (
      'shipper',
      'consignee',
      'notify_party',
      'port_of_loading',
      'port_of_discharge',
      'container_count',
      'gross_weight'
    )
  ),
  raw_value           text,
  normalized_value    text,
  numeric_value       numeric,
  confidence          numeric(4,3)
                      check (confidence between 0 and 1),
  source_page         int,
  extraction_attempt  int not null default 1,
  is_human_corrected  boolean not null default false,
  is_current          boolean not null default true,
  created_at          timestamptz not null default now()
);


-- ---------------------------------------------------------------------
-- Validation results
-- Outcome of extraction validation.
-- ---------------------------------------------------------------------

create table validation_results (
  id           uuid primary key default gen_random_uuid(),
  activity_id  uuid not null references activities(id),
  document_id  uuid not null references documents(id),
  result       validation_result not null,
  issues       jsonb not null default '[]'::jsonb,
  attempt      int not null default 1,
  created_at   timestamptz not null default now()
);


-- ---------------------------------------------------------------------
-- Comparison runs
-- One deterministic SI-vs-BL comparison run.
-- ---------------------------------------------------------------------

create table comparison_runs (
  id              uuid primary key default gen_random_uuid(),
  case_id         uuid not null references cases(id),
  activity_id     uuid references activities(id),
  si_document_id  uuid not null references documents(id),
  bl_document_id  uuid not null references documents(id),
  result          comparison_result not null,
  mismatch_count  int not null default 0,
  created_at      timestamptz not null default now()
);


-- ---------------------------------------------------------------------
-- Comparison items
-- Field-by-field comparison results.
-- ---------------------------------------------------------------------

create table comparison_items (
  id             uuid primary key default gen_random_uuid(),
  run_id         uuid not null references comparison_runs(id) on delete cascade,
  field_name     text not null,
  si_raw         text,
  bl_raw         text,
  si_normalized  text,
  bl_normalized  text,
  is_match       boolean not null,
  created_at     timestamptz not null default now()
);


-- ---------------------------------------------------------------------
-- Review tasks
-- Tasks routed to a human when automation cannot safely continue.
-- ---------------------------------------------------------------------

create table review_tasks (
  id           uuid primary key default gen_random_uuid(),
  case_id      uuid not null references cases(id),
  activity_id  uuid references activities(id),
  reason       text not null,
  description  text,
  context      jsonb not null default '{}'::jsonb,
  status       review_status not null default 'OPEN',
  assigned_to  uuid references case_officers(id),
  created_at   timestamptz not null default now(),
  assigned_at  timestamptz,
  resolved_at  timestamptz
);


-- ---------------------------------------------------------------------
-- Human decisions
-- Officer decision on a review task.
-- ---------------------------------------------------------------------

create table human_decisions (
  id                    uuid primary key default gen_random_uuid(),
  review_task_id        uuid not null references review_tasks(id),
  case_id               uuid not null references cases(id),
  officer_id            uuid not null references case_officers(id),
  decision              decision_type not null,
  notes                 text,
  payload               jsonb not null default '{}'::jsonb,
  resulting_document_id uuid references documents(id),
  created_at            timestamptz not null default now()
);


-- ---------------------------------------------------------------------
-- Agent actions
-- Every tool call proposed by the agent.
-- ---------------------------------------------------------------------

create table agent_actions (
  id                bigint generated always as identity primary key,
  case_id           uuid references cases(id),
  tool_name         text not null,
  tool_input        jsonb,
  tool_output       jsonb,
  allowed           boolean not null default true,
  rejection_reason  text,
  created_at        timestamptz not null default now()
);


-- ---------------------------------------------------------------------
-- Audit events
-- Append-only audit trail.
-- ---------------------------------------------------------------------

create table audit_events (
  id           bigint generated always as identity primary key,
  case_id      uuid references cases(id),
  actor        actor_type not null,
  actor_id     text,
  event_type   text not null,
  entity_type  text,
  entity_id    uuid,
  from_state   text,
  to_state     text,
  details      jsonb not null default '{}'::jsonb,
  created_at   timestamptz not null default now()
);


-- =====================================================================
-- 3. INDEXES
-- =====================================================================

create index idx_cases_shipment_ref
  on cases (shipment_ref);

create index idx_emails_case_id
  on emails (case_id);

create index idx_emails_thread_id
  on emails (gmail_thread_id);

create index idx_documents_case_id
  on documents (case_id);

create index idx_activities_case_state
  on activities (case_id, state);

create index idx_extracted_fields_doc
  on extracted_fields (document_id);

create index idx_validation_activity
  on validation_results (activity_id);

create index idx_comparison_runs_case
  on comparison_runs (case_id);

create index idx_comparison_items_run
  on comparison_items (run_id);

create index idx_review_tasks_status
  on review_tasks (status);

create index idx_review_tasks_case
  on review_tasks (case_id);

create index idx_human_decisions_task
  on human_decisions (review_task_id);

create index idx_agent_actions_case
  on agent_actions (case_id, created_at);

create index idx_audit_events_case
  on audit_events (case_id, created_at);


-- ---------------------------------------------------------------------
-- Document uniqueness rules
-- ---------------------------------------------------------------------

create unique index uq_documents_case_type_version
  on documents (case_id, doc_type, version)
  where doc_type in ('SI', 'BL');

create unique index uq_documents_current
  on documents (case_id, doc_type)
  where is_current
    and doc_type in ('SI', 'BL');


-- ---------------------------------------------------------------------
-- Extraction attempt uniqueness
-- ---------------------------------------------------------------------

create unique index uq_extracted_fields_attempt
  on extracted_fields (
    document_id,
    field_name,
    extraction_attempt
  );


-- =====================================================================
-- 4. TRIGGERS / FUNCTIONS
-- =====================================================================


-- ---------------------------------------------------------------------
-- Automatically update updated_at
-- ---------------------------------------------------------------------

create function set_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;


create trigger trg_cases_updated
before update on cases
for each row
execute function set_updated_at();


create trigger trg_activities_updated
before update on activities
for each row
execute function set_updated_at();


-- ---------------------------------------------------------------------
-- Prevent audit event modification/deletion
-- ---------------------------------------------------------------------

create function prevent_audit_changes()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  raise exception 'audit_events is append-only';
end;
$$;


create trigger trg_audit_events_immutable
before update or delete on audit_events
for each row
execute function prevent_audit_changes();


-- =====================================================================
-- 5. ROW LEVEL SECURITY
--
-- RLS is enabled with no public policies.
--
-- The frontend does NOT connect directly to Supabase.
-- The PHP backend connects using server-side credentials.
-- =====================================================================

alter table case_officers
  enable row level security;

alter table cases
  enable row level security;

alter table emails
  enable row level security;

alter table documents
  enable row level security;

alter table activities
  enable row level security;

alter table extracted_fields
  enable row level security;

alter table validation_results
  enable row level security;

alter table comparison_runs
  enable row level security;

alter table comparison_items
  enable row level security;

alter table review_tasks
  enable row level security;

alter table human_decisions
  enable row level security;

alter table agent_actions
  enable row level security;

alter table audit_events
  enable row level security;
```
