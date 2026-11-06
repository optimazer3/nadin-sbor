-- Схема базы для бота задач.
-- Выполните этот файл в Supabase: SQL Editor -> New query -> вставить -> Run.

create table if not exists public.tasks (
    id          bigint generated always as identity primary key,
    title       text        not null check (length(trim(title)) > 0),
    assignee    text,
    deadline    date,
    status      text        not null default 'new'
                check (status in ('new', 'in_progress', 'on_hold', 'done', 'cancelled')),
    source      text        not null default 'text'
                check (source in ('voice', 'text')),
    raw_text    text,               -- исходный текст / расшифровка голосового
    created_by  bigint,             -- Telegram ID того, кто создал задачу
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

create index if not exists tasks_status_idx   on public.tasks (status);
create index if not exists tasks_deadline_idx on public.tasks (deadline);

-- Автоматически обновляем updated_at при любом изменении строки.
create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists tasks_set_updated_at on public.tasks;
create trigger tasks_set_updated_at
    before update on public.tasks
    for each row execute function public.set_updated_at();

-- Включаем RLS без политик: анонимный (публичный) ключ не увидит задачи.
-- Бот работает с ключом service_role, который обходит RLS.
alter table public.tasks enable row level security;
