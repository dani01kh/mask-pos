-- Mask POS cloud sales/returns/shifts sync
--
-- Run this once in Supabase SQL Editor.
-- It applies events from public.pos_sync_events into the real tables used by
-- dashboard/website sections: sales, sale_items, returns, cash_shifts.
--
-- Collision safety:
-- Local IDs from different PCs can overlap. These triggers store the original
-- device/local IDs in cloud_* columns and allocate cloud IDs inside Supabase.

alter table public.sales add column if not exists cloud_event_id text;
alter table public.sales add column if not exists cloud_device_id text;
alter table public.sales add column if not exists cloud_local_id text;
drop index if exists public.sales_cloud_event_unique;
create unique index if not exists sales_cloud_event_unique
    on public.sales (cloud_event_id);

alter table public.sale_items add column if not exists cloud_event_id text;
alter table public.sale_items add column if not exists cloud_sale_event_id text;
alter table public.sale_items add column if not exists cloud_device_id text;
alter table public.sale_items add column if not exists cloud_local_id text;
drop index if exists public.sale_items_cloud_event_unique;
create unique index if not exists sale_items_cloud_event_unique
    on public.sale_items (cloud_event_id);

alter table public.returns add column if not exists cloud_event_id text;
alter table public.returns add column if not exists cloud_device_id text;
alter table public.returns add column if not exists cloud_local_id text;
alter table public.returns add column if not exists cloud_original_sale_local_id text;
drop index if exists public.returns_cloud_event_unique;
create unique index if not exists returns_cloud_event_unique
    on public.returns (cloud_event_id);

alter table public.return_items add column if not exists cloud_event_id text;
alter table public.return_items add column if not exists cloud_return_event_id text;
alter table public.return_items add column if not exists cloud_device_id text;
alter table public.return_items add column if not exists cloud_local_id text;
drop index if exists public.return_items_cloud_event_unique;
create unique index if not exists return_items_cloud_event_unique
    on public.return_items (cloud_event_id);

alter table public.cash_shifts add column if not exists opening_usd numeric;
alter table public.cash_shifts add column if not exists opening_lbp numeric;
alter table public.cash_shifts add column if not exists closing_usd numeric;
alter table public.cash_shifts add column if not exists closing_lbp numeric;
alter table public.cash_shifts add column if not exists lbp_per_usd numeric;
alter table public.cash_shifts add column if not exists cloud_device_id text;
alter table public.cash_shifts add column if not exists cloud_local_id text;
drop index if exists public.cash_shifts_cloud_unique;
create unique index if not exists cash_shifts_cloud_unique
    on public.cash_shifts (cloud_device_id, cloud_local_id);

alter table public.sales enable row level security;
alter table public.sale_items enable row level security;
alter table public.returns enable row level security;
alter table public.return_items enable row level security;
alter table public.cash_shifts enable row level security;

drop policy if exists "sales_anon_select" on public.sales;
create policy "sales_anon_select" on public.sales for select to anon using (true);

drop policy if exists "sale_items_anon_select" on public.sale_items;
create policy "sale_items_anon_select" on public.sale_items for select to anon using (true);

drop policy if exists "returns_anon_select" on public.returns;
create policy "returns_anon_select" on public.returns for select to anon using (true);

drop policy if exists "return_items_anon_select" on public.return_items;
create policy "return_items_anon_select" on public.return_items for select to anon using (true);

drop policy if exists "cash_shifts_anon_select" on public.cash_shifts;
create policy "cash_shifts_anon_select" on public.cash_shifts for select to anon using (true);

alter table public.employees add column if not exists cloud_device_id text;
alter table public.employees add column if not exists cloud_local_id text;
create unique index if not exists employees_name_unique
    on public.employees (name);

alter table public.employees enable row level security;

drop policy if exists "employees_anon_select" on public.employees;
create policy "employees_anon_select" on public.employees for select to anon using (true);

create or replace function public.maskpos_next_table_id(table_name text)
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
    n integer;
begin
    execute format('select coalesce(max(id), 0) + 1 from public.%I', table_name) into n;
    return n;
end;
$$;

create or replace function public.maskpos_apply_sale_event()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
    p jsonb;
    s jsonb;
    item jsonb;
    cloud_sale_id integer;
    cloud_item_id integer;
    i integer := 0;
begin
    if coalesce(new.entity_type, '') <> 'sale' then
        return new;
    end if;

    p := coalesce(new.payload, '{}'::jsonb);
    s := coalesce(p->'sale', '{}'::jsonb);

    if coalesce(new.event_type, '') = 'delete' then
        select id into cloud_sale_id
        from public.sales
        where cloud_device_id = new.device_id
          and cloud_local_id = coalesce(nullif(new.entity_id, ''), nullif(p->>'sale_id', ''), nullif(s->>'id', ''))
        limit 1;

        if cloud_sale_id is null then
            cloud_sale_id := coalesce(
                nullif(p->>'sale_id', '')::integer,
                nullif(s->>'id', '')::integer,
                nullif(new.entity_id, '')::integer
            );
        end if;

        delete from public.return_items
        where return_id in (
            select id from public.returns where original_sale_id = cloud_sale_id
        );
        delete from public.returns where original_sale_id = cloud_sale_id;
        delete from public.sale_items where sale_id = cloud_sale_id;
        delete from public.sales where id = cloud_sale_id;
        return new;
    end if;

    if coalesce(new.event_type, '') <> 'create' then
        return new;
    end if;

    select id into cloud_sale_id
    from public.sales
    where cloud_event_id = new.event_id
    limit 1;

    if cloud_sale_id is null then
        lock table public.sales in exclusive mode;
        cloud_sale_id := public.maskpos_next_table_id('sales');
    end if;

    insert into public.sales (
        id, created_at, total_amount, payment_method, customer_name, shift_id,
        subtotal, discount_total, tax_total, shipping, net_sales, total_sales,
        cash_paid, store_credit_used, is_exchange, receipt_date, receipt_seq,
        receipt_code, notes, cloud_event_id, cloud_device_id, cloud_local_id
    )
    values (
        cloud_sale_id,
        coalesce(nullif(s->>'created_at', ''), now()::text),
        coalesce(nullif(s->>'total_amount', ''), nullif(s->>'total_sales', ''), '0')::numeric,
        coalesce(nullif(s->>'payment_method', ''), nullif(p->>'payment_method', ''), 'CASH'),
        coalesce(s->>'customer_name', p->>'customer_name', ''),
        nullif(s->>'shift_id', '')::integer,
        coalesce(nullif(s->>'subtotal', ''), '0')::numeric,
        coalesce(nullif(s->>'discount_total', ''), '0')::numeric,
        coalesce(nullif(s->>'tax_total', ''), '0')::numeric,
        coalesce(nullif(s->>'shipping', ''), '0')::numeric,
        coalesce(nullif(s->>'net_sales', ''), nullif(s->>'total_sales', ''), '0')::numeric,
        coalesce(nullif(s->>'total_sales', ''), nullif(s->>'total_amount', ''), '0')::numeric,
        coalesce(nullif(s->>'cash_paid', ''), '0')::numeric,
        coalesce(nullif(s->>'store_credit_used', ''), '0')::numeric,
        coalesce(nullif(s->>'is_exchange', ''), '0')::integer,
        nullif(s->>'receipt_date', ''),
        nullif(s->>'receipt_seq', '')::integer,
        coalesce(s->>'receipt_code', ''),
        coalesce(s->>'notes', p->>'notes', ''),
        new.event_id,
        new.device_id,
        coalesce(new.entity_id, p->>'sale_id')
    )
    on conflict (cloud_event_id) do update
    set total_amount = excluded.total_amount,
        payment_method = excluded.payment_method,
        customer_name = excluded.customer_name,
        subtotal = excluded.subtotal,
        discount_total = excluded.discount_total,
        net_sales = excluded.net_sales,
        total_sales = excluded.total_sales,
        cash_paid = excluded.cash_paid,
        notes = excluded.notes
    returning id into cloud_sale_id;

    delete from public.sale_items where cloud_sale_event_id = new.event_id;

    for item in select * from jsonb_array_elements(coalesce(p->'items', '[]'::jsonb))
    loop
        i := i + 1;
        lock table public.sale_items in exclusive mode;
        cloud_item_id := public.maskpos_next_table_id('sale_items');

        insert into public.sale_items (
            id, sale_id, product_id, name, price, qty, line_total,
            gross_line_total, discount_allocated, cloud_event_id,
            cloud_sale_event_id, cloud_device_id, cloud_local_id
        )
        values (
            cloud_item_id,
            cloud_sale_id,
            -- Local POS product IDs do not match Supabase product IDs across PCs.
            -- Keep this NULL to avoid foreign-key failures; item name/price/qty remain intact.
            null,
            coalesce(item->>'name', ''),
            coalesce(nullif(item->>'price', ''), '0')::numeric,
            coalesce(nullif(item->>'qty', ''), '0')::integer,
            coalesce(nullif(item->>'line_total', ''), '0')::numeric,
            coalesce(nullif(item->>'gross_line_total', ''), nullif(item->>'line_total', ''), '0')::numeric,
            coalesce(nullif(item->>'discount_allocated', ''), '0')::numeric,
            new.event_id || ':' || i::text,
            new.event_id,
            new.device_id,
            item->>'id'
        );
    end loop;

    return new;
end;
$$;

create or replace function public.maskpos_apply_shift_event()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
    p jsonb;
    cloud_shift_id integer;
begin
    if coalesce(new.entity_type, '') <> 'shift' then
        return new;
    end if;

    p := coalesce(new.payload, '{}'::jsonb);

    select id into cloud_shift_id
    from public.cash_shifts
    where cloud_device_id = new.device_id
      and cloud_local_id = coalesce(new.entity_id, p->>'shift_id')
    limit 1;

    if cloud_shift_id is null then
        lock table public.cash_shifts in exclusive mode;
        cloud_shift_id := public.maskpos_next_table_id('cash_shifts');
    end if;

    insert into public.cash_shifts (
        id, opened_at, closed_at, opening_cash, closing_cash, notes,
        employee_id, shift_date, shift_seq, shift_code,
        opening_usd, opening_lbp, closing_usd, closing_lbp, lbp_per_usd,
        cloud_device_id, cloud_local_id
    )
    values (
        cloud_shift_id,
        coalesce(nullif(p->>'opened_at', ''), now()::text),
        nullif(p->>'closed_at', ''),
        coalesce(nullif(p->>'opening_cash', ''), '0')::numeric,
        nullif(p->>'closing_cash', '')::numeric,
        coalesce(p->>'notes', ''),
        nullif(p->>'employee_id', '')::integer,
        nullif(p->>'shift_date', ''),
        nullif(p->>'shift_seq', '')::integer,
        coalesce(p->>'shift_code', ''),
        coalesce(nullif(p->>'opening_usd', ''), '0')::numeric,
        coalesce(nullif(p->>'opening_lbp', ''), '0')::numeric,
        nullif(p->>'closing_usd', '')::numeric,
        nullif(p->>'closing_lbp', '')::numeric,
        coalesce(nullif(p->>'lbp_per_usd', ''), '89500')::numeric,
        new.device_id,
        coalesce(new.entity_id, p->>'shift_id')
    )
    on conflict (cloud_device_id, cloud_local_id) do update
    set closed_at = coalesce(excluded.closed_at, public.cash_shifts.closed_at),
        closing_cash = coalesce(excluded.closing_cash, public.cash_shifts.closing_cash),
        closing_usd = coalesce(excluded.closing_usd, public.cash_shifts.closing_usd),
        closing_lbp = coalesce(excluded.closing_lbp, public.cash_shifts.closing_lbp),
        lbp_per_usd = coalesce(excluded.lbp_per_usd, public.cash_shifts.lbp_per_usd),
        notes = coalesce(nullif(excluded.notes, ''), public.cash_shifts.notes);

    return new;
end;
$$;

create or replace function public.maskpos_apply_return_event()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
    p jsonb;
    line jsonb;
    cloud_return_id integer;
    cloud_return_item_id integer;
    cloud_original_sale_id integer;
    i integer := 0;
begin
    if coalesce(new.entity_type, '') <> 'return' or coalesce(new.event_type, '') <> 'create' then
        return new;
    end if;

    p := coalesce(new.payload, '{}'::jsonb);

    select id into cloud_original_sale_id
    from public.sales
    where cloud_device_id = new.device_id
      and cloud_local_id = p->>'original_sale_id'
    limit 1;

    select id into cloud_return_id
    from public.returns
    where cloud_event_id = new.event_id
    limit 1;

    if cloud_return_id is null then
        lock table public.returns in exclusive mode;
        cloud_return_id := public.maskpos_next_table_id('returns');
    end if;

    insert into public.returns (
        id, original_sale_id, created_at, total_return_amount, shift_id,
        notes, cloud_event_id, cloud_device_id, cloud_local_id,
        cloud_original_sale_local_id
    )
    values (
        cloud_return_id,
        coalesce(cloud_original_sale_id, nullif(p->>'original_sale_id', '')::integer),
        now()::text,
        coalesce(nullif(p->>'expected_total', ''), '0')::numeric,
        null,
        coalesce(p->>'notes', ''),
        new.event_id,
        new.device_id,
        coalesce(new.entity_id, p->>'return_id'),
        p->>'original_sale_id'
    )
    on conflict (cloud_event_id) do update
    set total_return_amount = excluded.total_return_amount,
        notes = excluded.notes;

    delete from public.return_items where cloud_return_event_id = new.event_id;

    for line in select * from jsonb_array_elements(coalesce(p->'returned_lines', '[]'::jsonb))
    loop
        i := i + 1;
        lock table public.return_items in exclusive mode;
        cloud_return_item_id := public.maskpos_next_table_id('return_items');

        insert into public.return_items (
            id, return_id, sale_item_id, product_id, name, price, qty, line_total,
            cloud_event_id, cloud_return_event_id, cloud_device_id, cloud_local_id
        )
        values (
            cloud_return_item_id,
            cloud_return_id,
            null,
            null,
            coalesce(line->>'name', ''),
            coalesce(nullif(line->>'price', ''), '0')::numeric,
            coalesce(nullif(line->>'qty', ''), '0')::integer,
            coalesce(nullif(line->>'line_total', ''), '0')::numeric,
            new.event_id || ':' || i::text,
            new.event_id,
            new.device_id,
            coalesce(line->>'id', line->>'sale_item_id')
        );
    end loop;

    return new;
end;
$$;

create or replace function public.maskpos_apply_employee_event()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
    p jsonb;
    employee_id integer;
begin
    if coalesce(new.entity_type, '') <> 'employee' then
        return new;
    end if;

    p := coalesce(new.payload, '{}'::jsonb);

    if nullif(trim(coalesce(p->>'name', new.entity_id, '')), '') is null then
        return new;
    end if;

    select id into employee_id
    from public.employees
    where name = trim(coalesce(p->>'name', new.entity_id, ''))
    limit 1;

    if employee_id is null then
        lock table public.employees in exclusive mode;
        employee_id := public.maskpos_next_table_id('employees');
    end if;

    insert into public.employees (
        id, name, pin, is_active, created_at, cloud_device_id, cloud_local_id
    )
    values (
        employee_id,
        trim(coalesce(p->>'name', new.entity_id, '')),
        coalesce(p->>'pin', ''),
        coalesce(nullif(p->>'is_active', ''), '1')::integer,
        coalesce(nullif(p->>'created_at', ''), now()::text),
        new.device_id,
        coalesce(new.entity_id, p->>'id')
    )
    on conflict (name) do update
    set pin = excluded.pin,
        is_active = excluded.is_active,
        cloud_device_id = excluded.cloud_device_id,
        cloud_local_id = excluded.cloud_local_id;

    return new;
end;
$$;

drop trigger if exists maskpos_sale_event_after_write on public.pos_sync_events;
create trigger maskpos_sale_event_after_write
after insert or update of payload, event_type, entity_type, entity_id
on public.pos_sync_events
for each row
execute function public.maskpos_apply_sale_event();

drop trigger if exists maskpos_shift_event_after_write on public.pos_sync_events;
create trigger maskpos_shift_event_after_write
after insert or update of payload, event_type, entity_type, entity_id
on public.pos_sync_events
for each row
execute function public.maskpos_apply_shift_event();

drop trigger if exists maskpos_return_event_after_write on public.pos_sync_events;
create trigger maskpos_return_event_after_write
after insert or update of payload, event_type, entity_type, entity_id
on public.pos_sync_events
for each row
execute function public.maskpos_apply_return_event();

drop trigger if exists maskpos_employee_event_after_write on public.pos_sync_events;
create trigger maskpos_employee_event_after_write
after insert or update of payload, event_type, entity_type, entity_id
on public.pos_sync_events
for each row
execute function public.maskpos_apply_employee_event();

-- Backfill events already uploaded before these triggers existed.
update public.pos_sync_events
set payload = payload
where entity_type in ('sale', 'shift', 'return', 'employee');
