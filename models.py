"""Shared data structures for the AutoTempest scraper pipeline."""
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Listing:
    title: str
    price: Optional[str] = None
    mileage: Optional[str] = None
    source_site: Optional[str] = None
    location: Optional[str] = None
    listing_url: Optional[str] = None
    image_url: Optional[str] = None
    vin: Optional[str] = None
    # Populated after VIN decode
    year: Optional[str] = None
    make: Optional[str] = None
    model: Optional[str] = None
    trim: Optional[str] = None
    options: list = field(default_factory=list)  # factory-installed options, if found

    def to_dict(self):
        return asdict(self)
