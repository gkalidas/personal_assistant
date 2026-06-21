"""
Face detection (InsightFace) + DBSCAN clustering.

Flow:
  1. scan_photo(path)        — detect faces, store embeddings in DB
  2. cluster_all()           — run DBSCAN over all stored embeddings, assign cluster IDs
  3. list_clusters()         — return cluster summary (name, count, exemplar photo)
  4. rename_cluster(id,name) — tag a cluster with a person's name

The InsightFace model (buffalo_sc — small/CPU-friendly) is loaded once and reused.
On first run it downloads ~85 MB of ONNX model files to ~/.insightface/.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import numpy as np

from modules.faces import db

log = logging.getLogger(__name__)

_app: Any = None  # lazy-loaded FaceAnalysis singleton


def _get_app():
    global _app
    if _app is None:
        from insightface.app import FaceAnalysis
        _app = FaceAnalysis(
            name="buffalo_sc",          # smallest model: detection + recognition
            providers=["CPUExecutionProvider"],
        )
        _app.prepare(ctx_id=0, det_size=(320, 320))
        log.info("InsightFace buffalo_sc model loaded")
    return _app


def scan_photo(photo_path: str) -> list[dict]:
    """
    Detect all faces in `photo_path`, store embeddings in DB.
    Returns list of face dicts: {face_idx, bbox, det_score, embedding, face_db_id}.
    Skips the photo silently if already in DB.
    """
    db.init()
    if db.already_processed(photo_path):
        return []

    try:
        from PIL import Image
        img = Image.open(photo_path).convert("RGB")
        # InsightFace expects BGR numpy array
        img_np = np.array(img)[:, :, ::-1]
    except Exception as e:
        log.warning("scan_photo: could not open %s: %s", photo_path, e)
        return []

    try:
        app    = _get_app()
        faces  = app.get(img_np)
    except Exception as e:
        log.error("InsightFace detection failed for %s: %s", photo_path, e)
        return []

    if not faces:
        # Record that we've processed this photo (zero faces) so we don't re-scan
        # We store a sentinel row with an empty embedding
        db.insert_face(photo_path, face_idx=-1, embedding=[], det_score=0.0)
        return []

    results = []
    for i, face in enumerate(faces):
        emb   = face.embedding.tolist() if face.embedding is not None else []
        bbox  = face.bbox.astype(int).tolist() if face.bbox is not None else None
        score = float(face.det_score) if face.det_score is not None else None

        if not emb:
            continue

        face_id = db.insert_face(
            photo_path, face_idx=i, embedding=emb, bbox=bbox, det_score=score
        )
        results.append({
            "face_idx":  i,
            "bbox":      bbox,
            "det_score": score,
            "embedding": emb,
            "face_db_id": face_id,
        })

    log.info("scan_photo: %s — %d face(s) detected", Path(photo_path).name, len(results))
    return results


def cluster_all(eps: float = 0.6, min_samples: int = 2) -> dict:
    """
    Run DBSCAN over all stored face embeddings (excluding sentinel rows).
    Assigns cluster_id to every face in the DB.
    Returns summary dict.

    eps=0.6 works well for normalised 512-d InsightFace embeddings (cosine ~ L2 on unit sphere).
    """
    from sklearn.cluster import DBSCAN
    from sklearn.preprocessing import normalize

    db.init()
    rows = db.get_all_embeddings()
    # Filter out sentinel rows (empty embedding)
    valid = [(r["id"], json.loads(r["embedding"])) for r in rows
             if r["embedding"] and r["embedding"] != "[]"]

    if len(valid) < 2:
        return {"clusters": 0, "faces_clustered": 0, "noise": len(valid)}

    ids       = [v[0] for v in valid]
    embeddings = np.array([v[1] for v in valid], dtype=np.float32)
    embeddings = normalize(embeddings)   # unit sphere → cosine ≈ euclidean

    labels = DBSCAN(eps=eps, min_samples=min_samples, metric="euclidean",
                    n_jobs=-1).fit_predict(embeddings)

    # Group face_ids by DBSCAN label
    groups: dict[int, list[int]] = {}
    for face_id, label in zip(ids, labels):
        groups.setdefault(int(label), []).append(face_id)

    n_clusters = sum(1 for k in groups if k >= 0)
    n_noise    = len(groups.get(-1, []))

    # Clear stale cluster data, then insert fresh rows and get real DB IDs
    db.clear_clusters()

    dbscan_to_db_id: dict[int, int] = {}
    for label, face_ids in groups.items():
        if label < 0:
            continue
        db_id = db.upsert_cluster(
            name="Unknown",
            exemplar_face_id=face_ids[0],
            face_count=len(face_ids),
        )
        dbscan_to_db_id[label] = db_id

    # Assign real DB cluster IDs back to faces
    for face_id, label in zip(ids, labels):
        db_cluster_id = dbscan_to_db_id.get(int(label))  # None for noise (label=-1)
        db.assign_cluster(face_id, db_cluster_id)

    log.info("cluster_all: %d clusters, %d noise faces", n_clusters, n_noise)
    return {"clusters": n_clusters, "faces_clustered": len(valid) - n_noise, "noise": n_noise}


def scan_directory(directory: str,
                   exts: tuple[str, ...] = (".jpg", ".jpeg", ".png", ".heic", ".webp")
                   ) -> dict:
    """Scan all photos in `directory` and return a summary."""
    db.init()
    photos  = [str(p) for p in Path(directory).rglob("*") if p.suffix.lower() in exts]
    total   = len(photos)
    scanned = 0
    faces   = 0
    for path in photos:
        if db.already_processed(path):
            continue
        result = scan_photo(path)
        scanned += 1
        faces   += len(result)
        log.debug("scanned %d/%d", scanned, total)

    return {"total_photos": total, "newly_scanned": scanned, "faces_found": faces}
