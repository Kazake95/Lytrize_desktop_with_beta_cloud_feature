-- Lytrize Supabase Setup
-- Run this ONCE in the Supabase SQL Editor (Dashboard → SQL Editor).
-- Re-running is safe — all statements use IF NOT EXISTS / CREATE OR REPLACE.

-- ─────────────────────────────────────────────────────────────────────────────
-- STEP 1: Disable email confirmation (desktop app — no email server needed)
-- Users can register and sign in immediately without confirming their email.
-- ─────────────────────────────────────────────────────────────────────────────

update auth.config set mailer_autoconfirm = true;

-- ─────────────────────────────────────────────────────────────────────────────
-- STEP 2: Tables
-- ─────────────────────────────────────────────────────────────────────────────

create table if not exists public.users (
    id           bigserial primary key,
    username     text unique not null,
    email        text unique not null,
    uuid         text unique,       -- Supabase auth.uid(), populated on first sync
    sync_enabled boolean not null default true,
    created_at   timestamptz not null default now()
);

create table if not exists public.sessions (
    id              bigserial primary key,
    user_id         bigint not null references public.users(id) on delete cascade,
    session_uuid    text unique not null,
    session_name    text not null default '',
    file_name       text,
    rows_count      integer,
    cols_count      integer,
    analysis_types  text,
    charts_json     text,
    dashboard_title text not null default '',
    kpis_json       text not null default '[]',
    layout_mode     text not null default 'portrait',
    source          text not null default 'local',
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- STEP 3: Indexes
-- ─────────────────────────────────────────────────────────────────────────────

create unique index if not exists idx_users_uuid        on public.users(uuid);
create index        if not exists idx_sessions_user_id  on public.sessions(user_id);
create unique index if not exists idx_sessions_uuid     on public.sessions(session_uuid);

-- ─────────────────────────────────────────────────────────────────────────────
-- STEP 4: Auto-update updated_at
-- ─────────────────────────────────────────────────────────────────────────────

create or replace function public.touch_updated_at()
returns trigger language plpgsql as $$
begin new.updated_at = now(); return new; end;
$$;

drop trigger if exists trg_sessions_updated_at on public.sessions;
create trigger trg_sessions_updated_at
before update on public.sessions
for each row execute function public.touch_updated_at();

-- ─────────────────────────────────────────────────────────────────────────────
-- STEP 5: Row Level Security
-- ─────────────────────────────────────────────────────────────────────────────

alter table public.users    enable row level security;
alter table public.sessions enable row level security;

-- users: each account can only read/write its own row.
-- SELECT/UPDATE also allow access via email for the bootstrap case where
-- uuid is still NULL (account created before first sync).

drop policy if exists "users_select_own" on public.users;
create policy "users_select_own" on public.users for select
using (uuid = auth.uid()::text or email = auth.email());

drop policy if exists "users_insert_own" on public.users;
create policy "users_insert_own" on public.users for insert
with check (uuid = auth.uid()::text or email = auth.email());

drop policy if exists "users_update_own" on public.users;
create policy "users_update_own" on public.users for update
using  (uuid = auth.uid()::text or (email = auth.email() and uuid is null))
with check (uuid = auth.uid()::text);

drop policy if exists "users_delete_own" on public.users;
create policy "users_delete_own" on public.users for delete
using (uuid = auth.uid()::text);

-- sessions: owned by the users row whose uuid = auth.uid().

drop policy if exists "sessions_select_own" on public.sessions;
create policy "sessions_select_own" on public.sessions for select
using (exists (select 1 from public.users u where u.id = user_id and u.uuid = auth.uid()::text));

drop policy if exists "sessions_insert_own" on public.sessions;
create policy "sessions_insert_own" on public.sessions for insert
with check (exists (select 1 from public.users u where u.id = user_id and u.uuid = auth.uid()::text));

drop policy if exists "sessions_update_own" on public.sessions;
create policy "sessions_update_own" on public.sessions for update
using  (exists (select 1 from public.users u where u.id = user_id and u.uuid = auth.uid()::text))
with check (exists (select 1 from public.users u where u.id = user_id and u.uuid = auth.uid()::text));

drop policy if exists "sessions_delete_own" on public.sessions;
create policy "sessions_delete_own" on public.sessions for delete
using (exists (select 1 from public.users u where u.id = user_id and u.uuid = auth.uid()::text));
