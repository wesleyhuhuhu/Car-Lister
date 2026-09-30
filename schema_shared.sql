-- Shared access for users and the listings page. Run this ONCE in the Supabase
-- SQL Editor (after schema.sql). Safe to run again.
--
-- Users get the public "anon" key. With it they can:
--   * read the public_listings view (safe columns only; this makes the listings public)
--   * call count_pending / claim_pending_vins / claim_vin / submit_options / release_vins
-- They cannot read, edit or delete the listings table itself, and submit_options
-- can only fill in a VIN that has no options yet (it never overwrites).

alter table listings add column if not exists claimed_by text;
alter table listings add column if not exists claimed_at timestamptz;
create index if not exists listings_pending_idx on listings (year) where build_sheet is null;

-- Comparing trims the same way lookup_matching.py does: ignore case and extra spaces.
create or replace function _norm(t text) returns text
language sql immutable as $$ select lower(regexp_replace(btrim(coalesce(t, '')), '\s+', ' ', 'g')) $$;

-- How much work is there? (read-only)
create or replace function count_pending(
    p_min_year int default 2022,
    p_trim text default 'M3 xDrive Competition',
    p_make text default 'BMW',
    p_claim_minutes int default 30
) returns table (pending bigint, claimed bigint, done bigint)
language sql security definer set search_path = public as $$
    select
        count(*) filter (where l.build_sheet is null and (l.claimed_at is null or l.claimed_at < now() - make_interval(mins => p_claim_minutes))),
        count(*) filter (where l.build_sheet is null and l.claimed_at >= now() - make_interval(mins => p_claim_minutes)),
        count(*) filter (where l.build_sheet is not null)
    from listings l
    where l.vin is not null and l.year >= p_min_year
      and _norm(l."trim") = _norm(p_trim) and upper(coalesce(l.make, '')) = upper(p_make)
$$;

-- Hand out up to p_limit VINs that still need a lookup, and mark them as taken
-- for p_claim_minutes so two helpers never get the same VIN.
create or replace function claim_pending_vins(
    p_worker text,
    p_limit int default 5,
    p_min_year int default 2022,
    p_trim text default 'M3 xDrive Competition',
    p_make text default 'BMW',
    p_claim_minutes int default 30
) returns table (listing_vin text, listing_title text, listing_year int, listing_trim text)
language plpgsql security definer set search_path = public as $$
begin
    if p_limit < 1 or p_limit > 25 then
        raise exception 'p_limit must be between 1 and 25';
    end if;
    return query
    with picked as (
        select l.listing_key
        from listings l
        where l.vin is not null and l.build_sheet is null
          and l.year >= p_min_year
          and _norm(l."trim") = _norm(p_trim)
          and upper(coalesce(l.make, '')) = upper(p_make)
          and (l.claimed_at is null or l.claimed_at < now() - make_interval(mins => p_claim_minutes))
        order by l.claimed_at nulls first, l.year desc, l.listing_key
        limit p_limit
        for update skip locked
    )
    update listings u
    set claimed_by = left(coalesce(p_worker, 'unknown'), 60), claimed_at = now()
    from picked
    where u.listing_key = picked.listing_key
    returning u.vin, u.title, u.year, u."trim";
end $$;

-- Store the result of one bimmer.work lookup. Returns false if the VIN is unknown
-- or already has options (nothing is overwritten).
create or replace function submit_options(p_vin text, p_build_sheet jsonb) returns boolean
language plpgsql security definer set search_path = public as $$
declare
    v_codes text[];
    v_options text[];
    v_rows int;
begin
    if p_build_sheet is null or jsonb_typeof(p_build_sheet -> 'Options') is distinct from 'object' then
        raise exception 'build_sheet must contain an Options object';
    end if;
    if length(p_build_sheet::text) > 200000 then
        raise exception 'build_sheet is too large';
    end if;
    select array_agg(key), array_agg(btrim(key || ' ' || coalesce(value #>> '{}', '')))
      into v_codes, v_options
      from jsonb_each(p_build_sheet -> 'Options');
    if v_codes is null then
        raise exception 'build_sheet has no options';
    end if;

    update listings
       set build_sheet = p_build_sheet,
           option_codes = v_codes,
           options = v_options,
           options_checked_at = now(),
           claimed_by = null,
           claimed_at = null,
           updated_at = now()
     where vin = upper(btrim(p_vin)) and build_sheet is null;
    get diagnostics v_rows = row_count;
    return v_rows > 0;
end $$;

-- Claim ONE specific VIN (the "Fetch options" button). True if this user may now
-- look it up: it exists, is a BMW, has no options yet, and nobody else holds a fresh
-- claim. Claiming again as the same user just refreshes the claim.
create or replace function claim_vin(p_worker text, p_vin text, p_claim_minutes int default 30)
returns boolean
language plpgsql security definer set search_path = public as $$
declare v_rows int;
begin
    update listings
       set claimed_by = left(coalesce(p_worker, 'unknown'), 60), claimed_at = now()
     where vin = upper(btrim(p_vin)) and build_sheet is null
       and upper(coalesce(make, '')) = 'BMW'
       and (claimed_at is null
            or claimed_at < now() - make_interval(mins => p_claim_minutes)
            or claimed_by = left(coalesce(p_worker, 'unknown'), 60));
    get diagnostics v_rows = row_count;
    return v_rows > 0;
end $$;

-- Give back VINs a user claimed but did not get to (stopped, or rate limited).
create or replace function release_vins(p_worker text, p_vins text[]) returns int
language plpgsql security definer set search_path = public as $$
declare v_rows int;
begin
    update listings
       set claimed_by = null, claimed_at = null
     where vin = any (select upper(btrim(v)) from unnest(p_vins) v)
       and claimed_by = left(coalesce(p_worker, 'unknown'), 60) and build_sheet is null;
    get diagnostics v_rows = row_count;
    return v_rows;
end $$;

-- What the listings page reads. Runs with the owner's rights, so the locked table
-- stays locked; only these columns are exposed (no worker names).
create or replace view public_listings with (security_invoker = false) as
select listing_key, vin, title, year, make, model, "trim",
       price, price_text, mileage, mileage_text, source_site, location, listing_url, image_url,
       options, option_codes, build_sheet, options_checked_at, first_seen_at, updated_at,
       (build_sheet is null and claimed_at is not null and claimed_at > now() - interval '30 minutes') as being_fetched
from listings;
revoke all on public_listings from public, anon, authenticated;
grant select on public_listings to anon, authenticated;

-- Functions are executable by everyone by default; allow only the intended ones.
revoke all on function _norm(text) from public, anon, authenticated;
revoke all on function count_pending(int, text, text, int) from public, anon, authenticated;
revoke all on function claim_pending_vins(text, int, int, text, text, int) from public, anon, authenticated;
revoke all on function claim_vin(text, text, int) from public, anon, authenticated;
revoke all on function submit_options(text, jsonb) from public, anon, authenticated;
revoke all on function release_vins(text, text[]) from public, anon, authenticated;
grant execute on function count_pending(int, text, text, int) to anon, authenticated;
grant execute on function claim_pending_vins(text, int, int, text, text, int) to anon, authenticated;
grant execute on function claim_vin(text, text, int) to anon, authenticated;
grant execute on function submit_options(text, jsonb) to anon, authenticated;
grant execute on function release_vins(text, text[]) to anon, authenticated;
