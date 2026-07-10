-- Mask POS cloud product sync
--
-- Run this in Supabase SQL Editor.
--
-- Why this exists:
-- The app is already allowed to insert into public.pos_sync_events.
-- This trigger lets Supabase apply those product events into public.products
-- on the server side, so the website can read public.products directly.
--
-- Safety rule:
-- An empty/new PC cannot wipe cloud products. Cloud products are only marked
-- deleted when a product DELETE event is inserted.

create unique index if not exists products_barcode_unique
    on public.products (barcode);

alter table public.products
    add column if not exists location text;

alter table public.products enable row level security;

drop policy if exists "products_anon_select" on public.products;
create policy "products_anon_select"
    on public.products
    for select
    to anon
    using (true);

create or replace function public.maskpos_apply_product_event()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
    p jsonb;
    bc text;
    product_name text;
    product_category text;
    product_brand text;
    product_location text;
    product_price numeric;
    product_stock integer;
    product_low integer;
    product_deleted integer;
    product_created text;
begin
    if coalesce(new.entity_type, '') <> 'product' then
        return new;
    end if;

    p := coalesce(new.payload, '{}'::jsonb);
    bc := nullif(trim(coalesce(p->>'barcode', new.entity_id, '')), '');

    if bc is null then
        return new;
    end if;

    if coalesce(new.event_type, '') = 'delete' then
        insert into public.products (
            barcode, name, category, brand, location, sell_price, stock_qty,
            low_stock_level, is_deleted, created_at
        )
        values (
            bc,
            coalesce(nullif(trim(p->>'name'), ''), 'Deleted product'),
            coalesce(p->>'category', ''),
            coalesce(p->>'brand', ''),
            coalesce(p->>'location', ''),
            coalesce(nullif(p->>'sell_price', ''), '0')::numeric,
            coalesce(nullif(p->>'stock_qty', ''), '0')::integer,
            coalesce(nullif(p->>'low_stock_level', ''), '0')::integer,
            1,
            coalesce(nullif(p->>'created_at', ''), now()::text)
        )
        on conflict (barcode) do update
        set is_deleted = 1;

        return new;
    end if;

    product_name := coalesce(nullif(trim(p->>'name'), ''), 'Cloud item');
    product_category := coalesce(p->>'category', '');
    product_brand := coalesce(p->>'brand', '');
    product_location := coalesce(p->>'location', '');
    product_price := coalesce(nullif(p->>'sell_price', ''), '0')::numeric;
    product_stock := coalesce(nullif(p->>'stock_qty', ''), '0')::integer;
    product_low := coalesce(nullif(p->>'low_stock_level', ''), '0')::integer;
    product_deleted := coalesce(nullif(p->>'is_deleted', ''), '0')::integer;
    product_created := coalesce(nullif(p->>'created_at', ''), now()::text);

    insert into public.products (
        barcode, name, category, brand, location, sell_price, stock_qty,
        low_stock_level, is_deleted, created_at
    )
    values (
        bc, product_name, product_category, product_brand, product_location, product_price,
        product_stock, product_low, product_deleted, product_created
    )
    on conflict (barcode) do update
    set name = excluded.name,
        category = excluded.category,
        brand = excluded.brand,
        location = excluded.location,
        sell_price = excluded.sell_price,
        stock_qty = excluded.stock_qty,
        low_stock_level = excluded.low_stock_level,
        is_deleted = excluded.is_deleted;

    return new;
end;
$$;

drop trigger if exists maskpos_product_event_after_write on public.pos_sync_events;
create trigger maskpos_product_event_after_write
after insert or update of payload, event_type, entity_type, entity_id
on public.pos_sync_events
for each row
execute function public.maskpos_apply_product_event();

-- Backfill product events that already exist in pos_sync_events.
update public.pos_sync_events
set payload = payload
where entity_type = 'product';
