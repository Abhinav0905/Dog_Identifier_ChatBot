"""
Geolocation handling: EXIF extraction, browser geolocation, manual fallback,
and Dharamsala jurisdiction verification.
"""

from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS
from typing import Optional
import io
import logging
import math
import config
from services.image_processing import register_heif_support

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dharamsala jurisdiction definition
# ---------------------------------------------------------------------------
# Centre: Dharamsala town (32.2196° N, 76.3234° E)
# Radius is environment-configurable. It is temporarily broad for QA and
# should be reduced before strict production enforcement.
DHARAMSALA_CENTER_LAT: float = 32.2196
DHARAMSALA_CENTER_LNG: float = 76.3234
DHARAMSALA_REGION_RADIUS_KM: float = config.DHARAMSALA_REGION_RADIUS_KM


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


def is_in_dharamsala_region(lat: float, lng: float) -> bool:
    """Return True if the coordinate falls within the Dharamsala Animal Rescue jurisdiction."""
    return (
        haversine_distance(lat, lng, DHARAMSALA_CENTER_LAT, DHARAMSALA_CENTER_LNG)
        <= DHARAMSALA_REGION_RADIUS_KM
    )


def build_jurisdiction_details(lat: float, lng: float, source: str) -> dict:
    """Build an auditable service-area decision for one verified coordinate."""
    distance_km = haversine_distance(lat, lng, DHARAMSALA_CENTER_LAT, DHARAMSALA_CENTER_LNG)
    in_jurisdiction = distance_km <= DHARAMSALA_REGION_RADIUS_KM
    return {
        "source": source,
        "lat": round(lat, 6),
        "lng": round(lng, 6),
        "distance_km": round(distance_km, 1),
        "allowed_radius_km": DHARAMSALA_REGION_RADIUS_KM,
        "in_jurisdiction": in_jurisdiction,
    }
