-- ContextLife sync schema draft (PostgreSQL)
-- This is a starting point for managed-cloud and BYODB modes.

create extension if not exists pgcrypto;

create table if not exists timeline_entries (
  id text primary key,
  user_id text not null default 'default',
  entry_type text not null default 'message', -- message|reminder
  text_content text not null default '',
  image_key text not null default '',
  status text not null default 'pending',
  reminder_key text not null default '',
  scene_description text not null default '',
  event_time timestamptz not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  version bigint not null default 1
);

create table if not exists evidence_cards (
  id text primary key,
  user_id text not null default 'default',
  parent_msg_id text not null,
  status text not null default 'pending',
  created_at timestamptz not null default now(),
  confirmed_at timestamptz,
  payload jsonb not null,
  version bigint not null default 1
);

create table if not exists sub_cards (
  id text primary key,
  user_id text not null default 'default',
  evidence_card_id text not null,
  parent_card_id text not null default '',
  category text not null,
  major_category text not null,
  title text not null default '',
  content jsonb not null default '{}'::jsonb,
  search_tags jsonb not null default '[]'::jsonb,
  status text not null default 'active',
  confirmed_at timestamptz,
  updated_at timestamptz not null default now(),
  version bigint not null default 1
);

create table if not exists attachments (
  id uuid primary key default gen_random_uuid(),
  user_id text not null default 'default',
  storage_key text not null unique,
  sha256 text not null,
  mime_type text not null,
  size_bytes bigint not null,
  width int,
  height int,
  ref_count int not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists daily_reviews (
  user_id text not null default 'default',
  review_date date not null,
  payload jsonb not null,
  updated_at timestamptz not null default now(),
  primary key (user_id, review_date)
);

create table if not exists tomorrow_plans (
  user_id text not null default 'default',
  plan_date date not null,
  payload jsonb not null,
  updated_at timestamptz not null default now(),
  primary key (user_id, plan_date)
);

create table if not exists character_plans (
  user_id text not null default 'default',
  plan_date date not null,
  payload jsonb not null,
  updated_at timestamptz not null default now(),
  primary key (user_id, plan_date)
);

create table if not exists metrics_daily (
  user_id text not null default 'default',
  metric_group text not null, -- health
  metric_date date not null,
  payload jsonb not null,
  updated_at timestamptz not null default now(),
  primary key (user_id, metric_group, metric_date)
);

create table if not exists chat_messages (
  id text primary key,
  user_id text not null default 'default',
  role text not null, -- user|assistant
  message_type text not null default 'chat', -- chat|reminder
  text_content text not null default '',
  image_key text not null default '',
  tool_calls jsonb not null default '[]'::jsonb,
  event_time timestamptz not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  version bigint not null default 1
);

create index if not exists idx_timeline_user_time on timeline_entries(user_id, event_time desc);
create index if not exists idx_sub_cards_user_major on sub_cards(user_id, major_category, category);
create index if not exists idx_sub_cards_user_updated on sub_cards(user_id, updated_at desc);
