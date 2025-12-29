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
    # Urban fabric
    1111: 5000,  # High density urban fabric
    1121: 2500,  # Medium density urban fabric
    1122: 1000,  # Low density urban fabric
    1123: 300,   # Isolated or very low density urban fabric
    1130: 200,   # Urban vegetation (aún en entorno urbano)
    1410: 200,   # Green urban areas
    1421: 800,   # Sport and leisure green (eventos / afluencia)
    1422: 1200,  # Sport and leisure built-up

    # Industrial / transport / infrastructure
    1210: 600,   # Industrial or commercial units
    1221: 200,   # Road and rail networks and associated land
    1222: 1200,  # Major stations
    1230: 600,   # Port areas
    1241: 300,   # Airport areas
    1242: 1500,  # Airport terminals

    # Extractive / degraded / construction
    1310: 50,    # Mineral extraction sites
    1320: 10,    # Dump sites
    1330: 100,   # Construction sites

    # Agriculture (baja densidad)
    2110: 20,    # Non irrigated arable land
    2120: 20,    # Permanently irrigated land
    2130: 10,    # Rice fields
    2210: 15,    # Vineyards
    2220: 15,    # Fruit trees and berry plantations
    2230: 15,    # Olive groves
    2310: 15,    # Pastures
    2410: 15,    # Annual crops associated with permanent crops
    2420: 15,    # Complex cultivation patterns
    2430: 10,    # Land principally occupied by agriculture
    2440: 10,    # Agro-forestry areas

    # Forest / natural (muy baja / casi 0, pero conservativo mínimo)
    3110: 5,     # Broad-leaved forest
    3120: 5,     # Coniferous forest
    3130: 5,     # Mixed forest
    3210: 5,     # Natural grassland
    3220: 2,     # Moors and heathland
    3230: 2,     # Sclerophyllous vegetation
    3240: 2,     # Transitional woodland shrub
    3310: 2,     # Beaches, dunes and sand plains
    3320: 1,     # Bare rock
    3330: 1,     # Sparsely vegetated areas
    3340: 1,     # Burnt areas
    3350: 0,     # Glaciers and perpetual snow

    # Wetlands / water
    4000: 0,     # Wetlands
    5110: 0,     # Water courses
    5120: 0,     # Water bodies
    5210: 0,     # Coastal lagoons
    5220: 0,     # Estuaries
    5230: 0,     # Sea and ocean
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
