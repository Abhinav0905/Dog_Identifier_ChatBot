"""
Geolocation handling: EXIF extraction, browser geolocation, manual fallback,
and Dharamsala jurisdiction verification.
"""

from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS
from typing import Optional
from functools import lru_cache
import io
import json
import logging
import math
from pathlib import Path
import config
from services.image_processing import register_heif_support

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dharamsala jurisdiction definition
# ---------------------------------------------------------------------------
# Deb's shared route map is represented as:
# 1. a polygon around the loop shown in the map, and
# 2. a small buffer around named checkpoints so village-center geocoding
#    remains tolerant of ordinary GPS drift.
#
# Coordinates are decimal degrees. The route starts/ends at the DAR/Rakkar
# area and follows the named map stops clockwise around the service loop.
DHARAMSALA_CENTER_LAT: float = 32.1971
DHARAMSALA_CENTER_LNG: float = 76.3901
DHARAMSALA_REGION_RADIUS_KM: float = config.DHARAMSALA_SERVICE_POINT_RADIUS_KM

SERVICE_AREA_CHECKPOINTS: tuple[dict, ...] = (
    {
        "name": "Dharamsala Animal Rescue / Slate Godam Road, Rakkar",
        "lat": 32.197100,
        "lng": 76.390100,
    },
    {
        "name": "Kharota",
        "lat": 32.214342,
        "lng": 76.379741,
    },
    {
        "name": "Khanyara",
        "lat": 32.212326,
        "lng": 76.367079,
    },
    {
        "name": "Gamru Village Road",
        "lat": 32.224931,
        "lng": 76.330061,
    },
    {
        "name": "Chakban Gharoh",
        "lat": 32.231183,
        "lng": 76.278244,
    },
    {
        "name": "Gaggal",
        "lat": 32.152293,
        "lng": 76.270266,
    },
    {
        "name": "Chakban Banwala",
        "lat": 32.169574,
        "lng": 76.301985,
    },
    {
        "name": "Yol",
        "lat": 32.181542,
        "lng": 76.373371,
    },
    {
        "name": "Chamunda Devi Temple, Padar",
        "lat": 32.148737,
        "lng": 76.417314,
    },
)

SERVICE_AREA_POLYGON: tuple[tuple[float, float], ...] = (
    (32.197100, 76.390100),  # DAR / Slate Godam Road, Rakkar
    (32.214342, 76.379741),  # Kharota
    (32.212326, 76.367079),  # Khanyara
    (32.224931, 76.330061),  # Gamru Village Road
    (32.231183, 76.278244),  # Chakban Gharoh
    (32.152293, 76.270266),  # Gaggal
    (32.169574, 76.301985),  # Chakban Banwala
    (32.181542, 76.373371),  # Yol
    (32.148737, 76.417314),  # Chamunda Devi Temple, Padar
)

# Simplified ADM0 boundary published by geoBoundaries under CC0 1.0.
INDIA_BOUNDARY_PATH = Path(__file__).with_name("india_boundary.geojson")
INDIA_BOUNDARY_SOURCE_URL = (
    "https://www.geoboundaries.org/api/current/gbOpen/IND/ADM0/"
)


def _get_gps_info(exif_data: dict) -> dict:
    """Extract GPS info from EXIF data."""
    gps_info = {}
    for key, val in exif_data.items():
        tag = GPSTAGS.get(key, key)
        gps_info[tag] = val
    return gps_info


def _convert_to_degrees(value) -> float:
    """Convert GPS coordinate tuple to decimal degrees."""
    try:
        d, m, s = value
        return float(d) + float(m) / 60.0 + float(s) / 3600.0
    except (TypeError, ValueError):
        return 0.0


def extract_exif_location(image_bytes: bytes) -> Optional[dict]:
    """Attempt to extract GPS coordinates from image EXIF data."""
    try:
        register_heif_support()
        img = Image.open(io.BytesIO(image_bytes))
        exif_raw = img.getexif()
        if not exif_raw:
            return None

        gps_raw = None
        try:
            gps_raw = exif_raw.get_ifd(34853)
        except (AttributeError, KeyError, TypeError, ValueError):
            gps_raw = exif_raw.get(34853)
        if gps_raw:
            gps = _get_gps_info(gps_raw)
        else:
            gps = {}

        exif_data = {}
        for tag_id, value in exif_raw.items():
            tag = TAGS.get(tag_id, tag_id)
            exif_data[tag] = value

        if not gps and "GPSInfo" in exif_data:
            gps = _get_gps_info(exif_data["GPSInfo"])

        if "GPSLatitude" not in gps or "GPSLongitude" not in gps:
            return None

        lat = _convert_to_degrees(gps["GPSLatitude"])
        lng = _convert_to_degrees(gps["GPSLongitude"])

        if gps.get("GPSLatitudeRef", "N") == "S":
            lat = -lat
        if gps.get("GPSLongitudeRef", "E") == "W":
            lng = -lng

        if lat == 0.0 and lng == 0.0:
            return None

        return {
            "lat": round(lat, 6),
            "lng": round(lng, 6),
            "source": "exif",
            "accuracy": None,
        }
    except Exception as exc:
        logger.warning("EXIF GPS extraction failed: %s", exc)
        return None


def truncate_precision(lat: float, lng: float, decimals: int = 4) -> tuple[float, float]:
    """Truncate location precision for privacy."""
    return round(lat, decimals), round(lng, decimals)


def haversine_distance(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Return the great-circle distance in km between two coordinate pairs (Haversine formula)."""
    R = 6371.0  # Earth mean radius in km
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _point_in_polygon(lat: float, lng: float, polygon: tuple[tuple[float, float], ...]) -> bool:
    """Return True when a coordinate is inside a lat/lng polygon."""
    inside = False
    x = lng
    y = lat
    j = len(polygon) - 1
    for i, (lat_i, lng_i) in enumerate(polygon):
        lat_j, lng_j = polygon[j]
        xi = lng_i
        yi = lat_i
        xj = lng_j
        yj = lat_j
        intersects = ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi
        )
        if intersects:
            inside = not inside
        j = i
    return inside


@lru_cache(maxsize=1)
def _india_boundary_polygons() -> tuple[tuple[tuple[tuple[float, float], ...], ...], ...]:
    """Load India ADM0 polygons as rings of (lat, lng) points."""
    try:
        payload = json.loads(INDIA_BOUNDARY_PATH.read_text(encoding="utf-8"))
        geometry = payload["features"][0]["geometry"]
        geometry_type = geometry["type"]
        coordinates = geometry["coordinates"]
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        logger.error("India boundary data could not be loaded: %s", exc)
        return ()

    if geometry_type == "Polygon":
        raw_polygons = [coordinates]
    elif geometry_type == "MultiPolygon":
        raw_polygons = coordinates
    else:
        logger.error("Unsupported India boundary geometry type: %s", geometry_type)
        return ()

    polygons = []
    for raw_polygon in raw_polygons:
        rings = []
        for raw_ring in raw_polygon:
            ring = tuple(
                (float(point[1]), float(point[0]))
                for point in raw_ring
                if isinstance(point, (list, tuple)) and len(point) >= 2
            )
            if len(ring) >= 3:
                rings.append(ring)
        if rings:
            polygons.append(tuple(rings))
    return tuple(polygons)


def india_boundary_available() -> bool:
    """Return True when the bundled India boundary can be loaded."""
    return bool(_india_boundary_polygons())


def is_in_india(lat: float, lng: float) -> bool:
    """Return True when coordinates fall within India's bundled ADM0 boundary."""
    try:
        lat = float(lat)
        lng = float(lng)
    except (TypeError, ValueError):
        return False
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return False

    for rings in _india_boundary_polygons():
        outer_ring, *holes = rings
        if not _point_in_polygon(lat, lng, outer_ring):
            continue
        if any(_point_in_polygon(lat, lng, hole) for hole in holes):
            continue
        return True
    return False


def nearest_service_area_checkpoint(lat: float, lng: float) -> dict:
    """Return the nearest named point from Deb's Dharamsala route map."""
    nearest = min(
        SERVICE_AREA_CHECKPOINTS,
        key=lambda point: haversine_distance(lat, lng, point["lat"], point["lng"]),
    )
    distance_km = haversine_distance(lat, lng, nearest["lat"], nearest["lng"])
    return {
        "name": nearest["name"],
        "lat": nearest["lat"],
        "lng": nearest["lng"],
        "distance_km": round(distance_km, 1),
    }


def _service_area_match(lat: float, lng: float) -> str | None:
    if _point_in_polygon(lat, lng, SERVICE_AREA_POLYGON):
        return "deb_route_polygon"
    nearest = nearest_service_area_checkpoint(lat, lng)
    if nearest["distance_km"] <= DHARAMSALA_REGION_RADIUS_KM:
        return "checkpoint_radius"
    return None


def is_in_dharamsala_region(lat: float, lng: float) -> bool:
    """Return True if the coordinate falls within the Dharamsala Animal Rescue jurisdiction."""
    return _service_area_match(lat, lng) is not None


def build_jurisdiction_details(lat: float, lng: float, source: str) -> dict:
    """Build an auditable service-area decision for one verified coordinate."""
    nearest = nearest_service_area_checkpoint(lat, lng)
    service_area_match = _service_area_match(lat, lng)
    in_jurisdiction = service_area_match is not None
    return {
        "source": source,
        "lat": round(lat, 6),
        "lng": round(lng, 6),
        "in_india": is_in_india(lat, lng),
        "distance_km": nearest["distance_km"],
        "allowed_radius_km": DHARAMSALA_REGION_RADIUS_KM,
        "in_jurisdiction": in_jurisdiction,
        "service_area_match": service_area_match or "outside_deb_route",
        "nearest_service_area": nearest["name"],
        "nearest_service_area_lat": nearest["lat"],
        "nearest_service_area_lng": nearest["lng"],
    }
