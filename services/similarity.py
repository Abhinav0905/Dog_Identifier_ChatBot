"""
Duplicate and similarity detection service.
Uses SHA-256 for exact duplicates and perceptual hashing for near-duplicates.
"""

import hashlib
import io
from PIL import Image
import imagehash
from typing import Optional
import database as db
from config import SIMILARITY_PHASH_THRESHOLD
from services.image_processing import register_heif_support


def compute_sha256(image_bytes: bytes) -> str:
    return hashlib.sha256(image_bytes).hexdigest()


def compute_phash(image_bytes: bytes) -> str:
    register_heif_support()
    img = Image.open(io.BytesIO(image_bytes))
    return str(imagehash.phash(img))


def check_exact_duplicate(sha256: str) -> Optional[dict]:
    """Check for exact duplicate via SHA-256."""
    existing = db.find_by_sha256(sha256)
    if existing:
        return {
            "incident_id": existing["incident_id"],
            "match_type": "exact",
            "score": 1.0,
        }
    return None


def check_similar_images(phash_str: str, exclude_id: str = None) -> list[dict]:
    """Check for near-duplicate images via perceptual hash hamming distance."""
    all_hashes = db.find_all_phashes()
    similar = []

    try:
        current_hash = imagehash.hex_to_hash(phash_str)
    except Exception:
        return []

    for record in all_hashes:
        if record["incident_id"] == exclude_id:
            continue
        try:
            stored_hash = imagehash.hex_to_hash(record["image_phash"])
            distance = int(current_hash - stored_hash)
            if distance <= SIMILARITY_PHASH_THRESHOLD:
                similarity_score = max(0, 1.0 - (distance / 64.0))
                similar.append({
                    "incident_id": record["incident_id"],
                    "match_type": "perceptual",
                    "distance": distance,
                    "score": float(round(similarity_score, 3)),
                })
        except Exception:
            continue

    similar.sort(key=lambda x: x["score"], reverse=True)
    return similar[:5]


def run_similarity_checks(image_bytes: bytes, sha256: str, phash_str: str, current_id: str = None) -> dict:
    """Run all similarity checks and return combined result."""
    exact = check_exact_duplicate(sha256)
    if exact:
        return {
            "is_exact_duplicate": True,
            "exact_match_id": exact["incident_id"],
            "similar_incidents": [],
            "message": f"This appears to be the same image as a previously reported incident (ID: {exact['incident_id'][:8]}...). The existing case is being tracked.",
        }

    similar = check_similar_images(phash_str, exclude_id=current_id)
    if similar:
        top = similar[0]
        return {
            "is_exact_duplicate": False,
            "exact_match_id": None,
            "similar_incidents": similar,
            "message": f"A potentially similar report was found (similarity: {top['score']:.0%}). This could be the same animal. The team will review both reports.",
        }

    return {
        "is_exact_duplicate": False,
        "exact_match_id": None,
        "similar_incidents": [],
        "message": "",
    }
