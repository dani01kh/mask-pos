-- Run this in Supabase SQL Editor so Mask POS can mirror products
-- into the real public.products table used by your website.

alter table public.products enable row level security;

create unique index if not exists products_barcode_unique
    on public.products (barcode);

alter table public.products
    add column if not exists location text;

drop policy if exists "products_anon_select" on public.products;
create policy "products_anon_select"
    on public.products
    for select
    to anon
    using (true);

drop policy if exists "products_anon_insert" on public.products;
create policy "products_anon_insert"
    on public.products
    for insert
    to anon
    with check (true);

drop policy if exists "products_anon_update" on public.products;
create policy "products_anon_update"
    on public.products
    for update
    to anon
    using (true)
    with check (true);
