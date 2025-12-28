from flask import Flask, request, jsonify
from flask_cors import CORS
import rasterio
from rasterio.mask import mask
from rasterio.warp import transform_geom
from shapely.geometry import Polygon, mapping
import numpy as np
import os
import pathlib
import requests

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

DATA_DIR = pathlib.Path(os.environ.get("DATA_DIR", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Local paths (inside container)
JRC_PATH = pathlib.Path(os.environ.get("JRC_PATH", str(DATA_DIR / "JRC-CENSUS_2021_100m.tif")))
LUISA_PATH = pathlib.Path(os.environ.get("LUISA_PATH", str(DATA_DIR / "LUISA_basemap_020321_50m.tif")))

# Optional URLs to download rasters at startup if not present
JRC_URL = os.environ.get("JRC_URL")   # e.g., https://.../JRC-CENSUS_2021_100m.tif
LUISA_URL = os.environ.get("LUISA_URL")

# LUISA → conservative density map (hab/km²). Extend as needed.
LUISA_DENSITY_MAP = {
    1: 50,      # Continuous urban fabric
    2: 200,     # Dense discontinuous urban fabric
    3: 100,     # Medium discontinuous urban fabric
    4: 50,      # Low discontinuous urban fabric
    5: 20,      # Industrial/commercial
    6: 5000,    # Sport/leisure facilities (example)
}

def _download_if_missing(path: pathlib.Path, url: str | None) -> None:
    if path.exists() or not url:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")

    app.logger.info(f"Downloading {url} -> {path}")
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
    tmp.replace(path)
    app.logger.info(f"Download complete: {path}")

def ensure_data():
    _download_if_missing(JRC_PATH, JRC_URL)
    _download_if_missing(LUISA_PATH, LUISA_URL)

def _polygon_to_geojson(polygon_coords):
    # polygon_coords is expected as [[lng, lat], ...]
    if not polygon_coords or len(polygon_coords) < 3:
        raise ValueError("Polígono inválido: requiere >= 3 puntos")

    # Close ring if needed
    if polygon_coords[0] != polygon_coords[-1]:
        polygon_coords = polygon_coords + [polygon_coords[0]]

    poly = Polygon(polygon_coords)
    if not poly.is_valid:
        # Attempt a simple fix
        poly = poly.buffer(0)
    return mapping(poly)

def _mask_with_reprojection(src, geom_wgs84):
    '''
    Reproject incoming geometry (assumed EPSG:4326) to raster CRS before masking.
    This avoids wrong results when rasters are not in WGS84.
    '''
    if src.crs is None:
        # If raster CRS unknown, try masking as-is (best-effort).
        return mask(src, [geom_wgs84], crop=True)

    raster_crs = src.crs.to_string()
    if raster_crs in ("EPSG:4326", "WGS84"):
        geom_raster = geom_wgs84
    else:
        geom_raster = transform_geom("EPSG:4326", raster_crs, geom_wgs84, precision=6)

    return mask(src, [geom_raster], crop=True)

@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "jrc_exists": JRC_PATH.exists(),
        "luisa_exists": LUISA_PATH.exists(),
        "data_dir": str(DATA_DIR),
    })

@app.post("/query-density")
def query_density():
    try:
        ensure_data()

        data = request.get_json(force=True, silent=False) or {}
        polygon_coords = data.get("polygon")

        geom_wgs84 = _polygon_to_geojson(polygon_coords)

        # --- JRC Census
        d_jrc_max = 0.0
        d_jrc_avg = 0.0

        if JRC_PATH.exists():
            try:
                with rasterio.open(JRC_PATH) as src:
                    out_image, _ = _mask_with_reprojection(src, geom_wgs84)
                    values = out_image[0]
                    # Heuristics: treat <=0 as nodata for population density; adjust if your dataset differs
                    valid = values[np.isfinite(values) & (values > 0)]
                    if valid.size > 0:
                        d_jrc_max = float(np.max(valid))
                        d_jrc_avg = float(np.mean(valid))
            except Exception as e:
                app.logger.exception(f"Error leyendo JRC: {e}")

        # --- LUISA conservative
        d_luisa_max = 0.0
        luisa_class = None

        if LUISA_PATH.exists():
            try:
                with rasterio.open(LUISA_PATH) as src:
                    out_image, _ = _mask_with_reprojection(src, geom_wgs84)
                    values = out_image[0]
                    valid = values[np.isfinite(values) & (values > 0)]

                    if valid.size > 0:
                        unique, counts = np.unique(valid.astype(int), return_counts=True)
                        dominant_class = int(unique[np.argmax(counts)])
                        luisa_class = dominant_class

                        # Conservative max among present classes
                        for cls in unique:
                            conservative = LUISA_DENSITY_MAP.get(int(cls), 0)
                            d_luisa_max = max(d_luisa_max, float(conservative))
            except Exception as e:
                app.logger.exception(f"Error leyendo LUISA: {e}")

        d_popmax = max(d_jrc_max, d_luisa_max)

        return jsonify({
            "d_popmax": d_popmax,
            "d_avg": d_jrc_avg,
            "d_jrc_max": d_jrc_max,
            "d_luisa_conservative": d_luisa_max,
            "luisa_class": luisa_class
        })

    except Exception as e:
        app.logger.exception(f"Error: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), debug=True)
