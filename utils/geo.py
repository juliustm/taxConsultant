# utils/geo.py
"""
Where a receipt was collected, worked out offline.

Devices attach a coordinate pair to a submission. Turning that into something a human
can group by - a region name - is normally a call to a geocoding service, and this
module deliberately does not make one.

Why not: a network call per receipt puts a rate limit, an API key, an outage mode and
a third party in the middle of a pipeline that is otherwise exact and offline, and it
hands the location of every purchase the business makes to somebody else. The question
actually being asked is "which region was this spent in", and Tanzania has thirty-one
of them, so a table answers it with no dependency at all.

What that costs: the region is decided by the nearest regional centre, so a point near
a boundary can be attributed to the wrong side of it, and a point outside Tanzania is
attributed to the nearest Tanzanian region unless it is implausibly far. Every caller
labels the result approximate, and nothing downstream treats it as a fact from the
receipt - unlike everything in utils/tra_parser, this is inferred.
"""
import math
import re

# Approximate centre of each region, as (latitude, longitude). Regional centres rather
# than true polygon centroids: they are stable, checkable against a map, and accurate
# enough to bucket a purchase by region.
REGION_CENTRES = {
    'Arusha': (-3.3869, 36.6830),
    'Dar es Salaam': (-6.7924, 39.2083),
    'Dodoma': (-6.1630, 35.7516),
    'Geita': (-2.8725, 32.2317),
    'Iringa': (-7.7700, 35.6900),
    'Kagera': (-1.3320, 31.8120),
    'Katavi': (-6.3450, 31.0700),
    'Kigoma': (-4.8824, 29.6615),
    'Kilimanjaro': (-3.3400, 37.3400),
    'Lindi': (-9.9986, 39.7167),
    'Manyara': (-4.2200, 35.7500),
    'Mara': (-1.5000, 33.8000),
    'Mbeya': (-8.9000, 33.4500),
    'Morogoro': (-6.8210, 37.6610),
    'Mtwara': (-10.2667, 40.1833),
    'Mwanza': (-2.5164, 32.9175),
    'Njombe': (-9.3333, 34.7667),
    'Pwani': (-6.7700, 38.9200),
    'Rukwa': (-7.9667, 31.6167),
    'Ruvuma': (-10.6833, 35.6500),
    'Shinyanga': (-3.6614, 33.4210),
    'Simiyu': (-2.8000, 33.9833),
    'Singida': (-4.8167, 34.7500),
    'Songwe': (-9.1000, 32.9333),
    'Tabora': (-5.0167, 32.8000),
    'Tanga': (-5.0689, 39.0988),
    'Kaskazini Unguja': (-5.9333, 39.3000),
    'Kusini Unguja': (-6.3000, 39.4000),
    'Mjini Magharibi': (-6.1650, 39.2026),
    'Kaskazini Pemba': (-5.0560, 39.7280),
    'Kusini Pemba': (-5.2450, 39.7660),
}

# Past this from every regional centre, a point is reported as outside the country
# rather than being forced into the nearest region. No Tanzanian region reaches this
# far from its own centre, so it never rejects a domestic purchase.
MAX_REGION_DISTANCE_KM = 250

# Tanzania's bounding box, used to place a point on the plain SVG map.
BOUNDS = {'min_lat': -11.75, 'max_lat': -0.95, 'min_lng': 29.30, 'max_lng': 40.50}

EARTH_RADIUS_KM = 6371.0

# A signed decimal, which is all a coordinate ever is.
_NUMBER = re.compile(r'-?\d+(?:\.\d+)?')


def parse_location(value):
    """
    (latitude, longitude) from whatever the device attached, or None.

    Submissions carry location as free text - devices send 'lat,lng', some append a
    place name after a dash. Only the part before the dash is read, so a street name
    with a number in it cannot be mistaken for a coordinate.
    """
    if not value:
        return None

    head = str(value).split(' - ')[0]
    numbers = _NUMBER.findall(head)
    if len(numbers) < 2:
        return None

    latitude, longitude = float(numbers[0]), float(numbers[1])
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return latitude, longitude


def distance_km(first, second):
    """Great-circle distance between two (lat, lng) pairs, in kilometres."""
    if first is None or second is None:
        return None

    lat1, lng1 = math.radians(first[0]), math.radians(first[1])
    lat2, lng2 = math.radians(second[0]), math.radians(second[1])

    inner = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(inner)))


def region_for(point):
    """
    The name of the nearest region, or None when the point is not in Tanzania.

    Approximate by construction: see the module docstring. Near a boundary this can
    name the neighbouring region.
    """
    if point is None:
        return None

    name, separation = nearest_region(point)
    return None if separation is None or separation > MAX_REGION_DISTANCE_KM else name


def nearest_region(point):
    """(name, distance_km) of the closest regional centre, whatever the distance."""
    if point is None:
        return None, None

    return min(
        ((name, distance_km(point, centre)) for name, centre in REGION_CENTRES.items()),
        key=lambda entry: entry[1],
    )


def median_point(points):
    """
    The typical position among several points, or None.

    Median rather than mean, and the distinction matters: a mean is dragged towards
    whatever is furthest away, so one receipt collected in Mwanza moves the "usual
    place" of three Dar es Salaam receipts far enough that all three then look like
    outliers themselves. The median barely moves, which is the whole point of asking
    where a supplier's receipts *usually* come from.

    Each coordinate is taken separately. That is not a true geometric median, but
    Tanzania is small enough and far enough from the poles and the antimeridian that
    the difference is well inside the accuracy of a phone's GPS fix.
    """
    usable = [point for point in points if point is not None]
    if not usable:
        return None

    return (
        _median(sorted(point[0] for point in usable)),
        _median(sorted(point[1] for point in usable)),
    )


def _median(ordered):
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def to_svg_xy(point, width=100, height=100):
    """
    A point placed on a plain equirectangular canvas covering Tanzania.

    Enough for a scatter of where money is being spent, and it needs no tile server,
    no API key and no network. Points outside the country's box are clamped to its
    edge rather than dropped, so nothing silently disappears off the map.
    """
    if point is None:
        return None

    latitude = min(BOUNDS['max_lat'], max(BOUNDS['min_lat'], point[0]))
    longitude = min(BOUNDS['max_lng'], max(BOUNDS['min_lng'], point[1]))

    x = (longitude - BOUNDS['min_lng']) / (BOUNDS['max_lng'] - BOUNDS['min_lng']) * width
    # Latitude grows northward and SVG's y grows downward, so the axis is inverted.
    y = (BOUNDS['max_lat'] - latitude) / (BOUNDS['max_lat'] - BOUNDS['min_lat']) * height
    return round(x, 2), round(y, 2)
