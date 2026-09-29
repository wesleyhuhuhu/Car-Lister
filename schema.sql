-- Car listings table (PostgreSQL: Supabase, Neon, etc.). Safe to run more than once.

create table if not exists listings (
    listing_key        text primary key,          -- the VIN, or 'url:<link>' for the rare listing without one
    vin                text,
    title              text not null,
    year               integer,
    make               text,
    model              text,
    trim               text,
    price              integer,                   -- parsed from price_text, for sorting/filtering
    price_text         text,
    mileage            integer,                   -- parsed from mileage_text
    mileage_text       text,
    source_site        text,
    location           text,
    listing_url        text,
    image_url          text,
    options            text[]  not null default '{}',   -- display strings, e.g. '248 Steering Wheel Heating'
    option_codes       text[]  not null default '{}',   -- just the codes, e.g. '248' (filter on this)
    build_sheet        jsonb,                     -- full bimmer.work result; null = not looked up yet
    options_checked_at timestamptz,
    first_seen_at      timestamptz not null default now(),
    updated_at         timestamptz not null default now()
);

create index if not exists listings_option_codes_idx on listings using gin (option_codes);
create index if not exists listings_vehicle_idx      on listings (make, model, trim, year);
create index if not exists listings_price_idx        on listings (price);
create index if not exists listings_vin_idx          on listings (vin);

-- Lock the table down: with row level security on and no policies, the public
-- API key can't read or write anything. Your sync script connects with the
-- database password, which bypasses this. To let a website read listings
-- publicly, uncomment:
--   create policy "public read" on listings for select using (true);
alter table listings enable row level security;
