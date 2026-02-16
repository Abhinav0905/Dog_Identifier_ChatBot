"""
Geolocation handling: EXIF extraction, browser geolocation, manual fallback.
"""

from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS
from typing import Optional
import io


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
        img = Image.open(io.BytesIO(image_bytes))
        exif_raw = img._getexif()
        if not exif_raw:
            return None

        exif_data = {}
        for tag_id, value in exif_raw.items():
            tag = TAGS.get(tag_id, tag_id)
            exif_data[tag] = value

        if "GPSInfo" not in exif_data:
            return None

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
    except Exception:
        return None


def truncate_precision(lat: float, lng: float, decimals: int = 4) -> tuple[float, float]:
    """Truncate location precision for privacy."""
    return round(lat, decimals), round(lng, decimals)
