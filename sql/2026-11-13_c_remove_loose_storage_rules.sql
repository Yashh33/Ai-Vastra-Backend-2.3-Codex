-- 2026-11-13 (C): remove three loose storage rules that let ANY logged-in user read every
-- shop's fabric and hero images, and upload into the fabric bucket anywhere.
--
-- APPROVED EXCEPTION to add-only: this DROPS three storage.objects policies:
--   "Allow authenticated fabric reads"      (select, fabric-images, any path)
--   "Allow authenticated hero image reads"  (select, hero-images, any path)
--   "Allow authenticated fabric uploads"    (insert, fabric-images, any path)
--
-- These rules stay UNTOUCHED:
--   the four ai_vastra_storage_*_shop_scoped rules (each shop's own folder),
--   "Allow temp hero uploads",
--   "Allow authenticated output reads",
--   "Public read generated-outputs".
-- The TV app and the look thumbnail path rule only use generated-outputs: not affected.
--
-- Checked in this repo on 2026-11-13: the admin panel never reads or writes the fabric-images
-- or hero-images buckets with an anon or logged-in Supabase client (it gets every image
-- through the backend's /admin endpoints; adminPanel/src/lib/storage.ts has an unused
-- createSignedUrl helper on the anon client that nothing calls). The backend and worker use
-- the service role, which these rules do not affect.
--
-- WHEN: AFTER file B (2026-11-13_b_lock_backend_only_functions.sql) is tested, at a quiet time.
-- Run it one step at a time in the Supabase SQL editor.
-- If the SQL editor refuses to drop a storage rule (permission error on storage.objects),
-- delete the same three rules by name in Supabase > Storage > Policies instead.

-- STEP 1, read-only: must return 0. If it does not, STOP and report the number.
select count(*) as fabric_rows_outside_own_folder
from public.fabric_images where storage_path not like shop_id::text || '/%';

-- STEP 2
drop policy if exists "Allow authenticated fabric reads" on storage.objects;
drop policy if exists "Allow authenticated hero image reads" on storage.objects;
drop policy if exists "Allow authenticated fabric uploads" on storage.objects;

-- STEP 3: in the web app (demo shop), open the fabric list and a garment's hero images, upload
-- one fabric photo, and generate one look. If anything fails, run the UNDO below.

-- UNDO (puts the three rules back exactly). Copy, remove the leading "-- ", run:
-- create policy "Allow authenticated fabric reads" on storage.objects for select to authenticated using (bucket_id = 'fabric-images');
-- create policy "Allow authenticated hero image reads" on storage.objects for select to authenticated using (bucket_id = 'hero-images');
-- create policy "Allow authenticated fabric uploads" on storage.objects for insert to authenticated with check (bucket_id = 'fabric-images');
