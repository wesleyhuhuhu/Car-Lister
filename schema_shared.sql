-- Shared access for users and the listings page. Run this ONCE in the Supabase
-- SQL Editor (after schema.sql). Safe to run again.
--
-- Users get the public "anon" key. With it they can:
--   * read the public_listings view (safe columns only; this makes the listings public)
--   * call count_pending / claim_pending_vins / claim_vin / submit_options / release_vins / submit_listings
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

-- Contributors without the secret key can add scraped listings. Insert-only in spirit:
--   * new listings are added (any number overall; up to 500 per call, the scripts send 200 at a time)
--   * for a listing already in the table only the changing facts are refreshed (price, mileage,
--     location, link, photo); year/make/model/trim are filled in only where still empty
--   * options and build sheets are never touched, nothing can be deleted
-- Everything is validated server-side; the row key is computed here, never trusted from the caller.
create or replace function _to_int(t text, lo bigint default 0, hi bigint default 2000000000) returns integer
language sql immutable as $$
    select case when t ~ '^[0-9]{1,10}$' and t::bigint between lo and hi then t::integer end
$$;

create or replace function submit_listings(p_rows jsonb)
returns table (inserted int, updated int, skipped int)
language plpgsql security definer set search_path = public as $$
declare
    r jsonb; v_vin text; v_url text; v_img text; v_title text; v_key text; v_new boolean;
    v_ins int := 0; v_upd int := 0; v_skip int := 0;
begin
    if p_rows is null or jsonb_typeof(p_rows) <> 'array' then
        raise exception 'p_rows must be a JSON array';
    end if;
    if jsonb_array_length(p_rows) > 500 then
        raise exception 'send at most 500 listings per call';
    end if;
    if length(p_rows::text) > 3000000 then
        raise exception 'payload is too large';
    end if;

    for r in select value from jsonb_array_elements(p_rows) loop
        if jsonb_typeof(r) <> 'object' then v_skip := v_skip + 1; continue; end if;
        v_vin := upper(btrim(coalesce(r->>'vin', '')));
        if v_vin !~ '^[A-HJ-NPR-Z0-9]{17}$' then v_vin := null; end if;
        v_url := left(btrim(coalesce(r->>'listing_url', '')), 1000);
        if v_url !~* '^https?://' then v_url := null; end if;
        v_img := left(btrim(coalesce(r->>'image_url', '')), 1000);
        if v_img !~* '^https?://' then v_img := null; end if;
        v_title := left(btrim(coalesce(r->>'title', '')), 300);
        v_key := coalesce(v_vin, case when v_url is not null then 'url:' || v_url end);
        if v_key is null or v_title = '' then v_skip := v_skip + 1; continue; end if;

        insert into listings (listing_key, vin, title, year, make, model, "trim",
                              price, price_text, mileage, mileage_text, source_site, location, listing_url, image_url)
        values (v_key, v_vin, v_title, _to_int(r->>'year', 1900, 2100),
                left(nullif(btrim(r->>'make'), ''), 60), left(nullif(btrim(r->>'model'), ''), 100), left(nullif(btrim(r->>'trim'), ''), 100),
                _to_int(r->>'price'), left(nullif(btrim(r->>'price_text'), ''), 60),
                _to_int(r->>'mileage'), left(nullif(btrim(r->>'mileage_text'), ''), 60),
                left(nullif(btrim(r->>'source_site'), ''), 100), left(nullif(btrim(r->>'location'), ''), 200), v_url, v_img)
        on conflict (listing_key) do update set
            price        = coalesce(excluded.price, listings.price),
            price_text   = coalesce(excluded.price_text, listings.price_text),
            mileage      = coalesce(excluded.mileage, listings.mileage),
            mileage_text = coalesce(excluded.mileage_text, listings.mileage_text),
            location     = coalesce(excluded.location, listings.location),
            listing_url  = coalesce(excluded.listing_url, listings.listing_url),
            image_url    = coalesce(excluded.image_url, listings.image_url),
            year         = coalesce(listings.year, excluded.year),
            make         = coalesce(listings.make, excluded.make),
            model        = coalesce(listings.model, excluded.model),
            "trim"       = coalesce(listings."trim", excluded."trim"),
            updated_at   = now()
        returning (xmax = 0) into v_new;
        if v_new then v_ins := v_ins + 1; else v_upd := v_upd + 1; end if;
    end loop;
    return query select v_ins, v_upd, v_skip;
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
revoke all on function _to_int(text, bigint, bigint) from public, anon, authenticated;
revoke all on function submit_listings(jsonb) from public, anon, authenticated;
grant execute on function count_pending(int, text, text, int) to anon, authenticated;
grant execute on function claim_pending_vins(text, int, int, text, text, int) to anon, authenticated;
grant execute on function claim_vin(text, text, int) to anon, authenticated;
grant execute on function submit_options(text, jsonb) to anon, authenticated;
grant execute on function release_vins(text, text[]) to anon, authenticated;
grant execute on function submit_listings(jsonb) to anon, authenticated;
