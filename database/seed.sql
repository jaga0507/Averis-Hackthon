```sql
-- =====================================================================
-- TitanPort Demo Seed Data
-- Supabase PostgreSQL
--
-- Run AFTER schema.sql
--
-- Safe to run multiple times.
-- =====================================================================


-- =====================================================================
-- DEMO CASE OFFICERS
-- =====================================================================

insert into case_officers (
  id,
  full_name,
  email,
  can_authorize_corrections
)
values
  (
    'a0000000-0000-0000-0000-000000000001',
    'Aisha Rahman',
    'aisha.rahman@example.com',
    true
  ),
  (
    'a0000000-0000-0000-0000-000000000002',
    'Daniel Lim',
    'daniel.lim@example.com',
    false
  )
on conflict (id) do nothing;
```
