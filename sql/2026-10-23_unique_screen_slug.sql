-- 2026-10-23: unique TV slug per shop (case-insensitive).
-- ADD-ONLY. Creates one index; renames/drops nothing; resolve_shop_by_slug is untouched.
-- The admin API already rejects duplicate slugs, so this is a safety net for races.
-- Run the three steps in order in the Supabase SQL editor.

-- Step 1: existing indexes on shops. If a unique index on lower(screen_slug) is already
-- listed, you can stop here.
select indexname, indexdef
from pg_indexes
where schemaname = 'public' and tablename = 'shops';

-- Step 2: duplicate slugs (ignoring case). This MUST return 0 rows before step 3.
-- If it returns rows, give those shops different slugs in the admin panel first.
select lower(screen_slug) as slug, count(*) as shops, array_agg(id) as shop_ids
from public.shops
where screen_slug is not null
group by lower(screen_slug)
having count(*) > 1;

-- Step 3: the unique index (only for shops that have a slug).
create unique index if not exists shops_screen_slug_lower_key
  on public.shops (lower(screen_slug))
  where screen_slug is not null;
