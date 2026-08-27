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

# --- Remote COG (AESA DT-29) support -----------------------------------------
# GDAL/rasterio read remote Cloud Optimized GeoTIFFs through /vsicurl/.
# These options make the range requests efficient and avoid directory listing.
os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.tiff")
os.environ.setdefault("GDAL_HTTP_MULTIPLEX", "YES")
os.environ.setdefault("GDAL_HTTP_VERSION", "2")
os.environ.setdefault("VSI_CACHE", "TRUE")
os.environ.setdefault("VSI_CACHE_SIZE", "50000000")
os.environ.setdefault("GDAL_CACHEMAX", "512")

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


def _mask_with_reprojection(src, geom_wgs84, all_touched=False):
    '''
    Reproject incoming geometry (assumed EPSG:4326) to raster CRS before masking.
    This avoids wrong results when rasters are not in WGS84.
    '''
    if src.crs is None:
        # If raster CRS unknown, try masking as-is (best-effort).
        return mask(src, [geom_wgs84], crop=True, all_touched=all_touched)

    raster_crs = src.crs.to_string()
    if raster_crs in ("EPSG:4326", "WGS84"):
        geom_raster = geom_wgs84
    else:
        geom_raster = transform_geom("EPSG:4326", raster_crs, geom_wgs84, precision=6)

    return mask(src, [geom_raster], crop=True, all_touched=all_touched)


def _vsicurl(url: str) -> str:
    """Turn an http(s) URL into a GDAL /vsicurl/ path."""
    if url.startswith("/vsicurl/"):
        return url
    return "/vsicurl/" + url


def _stats_from_raster(path_or_vsi, geom_wgs84, all_touched=False):
    """Return (max, avg, n_pixels) of valid values inside the polygon."""
    with rasterio.open(path_or_vsi) as src:
        out_image, _ = _mask_with_reprojection(src, geom_wgs84, all_touched=all_touched)
        values = out_image[0].astype("float64")

        nodata = src.nodata
        valid_mask = np.isfinite(values)
        if nodata is not None:
            valid_mask &= values != nodata
        # Population density: negative values are invalid; keep 0 out of the
        # average so empty cells don't dilute the statistic.
        valid_mask &= values > 0

        valid = values[valid_mask]
        if valid.size == 0:
            return 0.0, 0.0, 0
        return float(np.max(valid)), float(np.mean(valid)), int(valid.size)


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "jrc_exists": JRC_PATH.exists(),
        "luisa_exists": LUISA_PATH.exists(),
        "data_dir": str(DATA_DIR),
        "remote_raster_support": True,
    })


@app.post("/query-density")
def query_density():
    try:
        data = request.get_json(force=True, silent=False) or {}
        polygon_coords = data.get("polygon")
        raster_url = data.get("raster_url")
        layer_id = data.get("layer_id")

        geom_wgs84 = _polygon_to_geojson(polygon_coords)

        # --- Case A: explicit remote raster (AESA DT-29 COG via /vsicurl/) ----
        if raster_url:
            try:
                d_max, d_avg, n = _stats_from_raster(
                    _vsicurl(raster_url), geom_wgs84, all_touched=True
                )
                return jsonify({
                    "d_popmax": d_max,
                    "d_avg": d_avg,
                    "d_jrc_max": d_max,
                    "d_luisa_conservative": 0.0,
                    "luisa_class": None,
                    "pixel_count": n,
                    # Echoing these two fields tells the app the layer was applied
                    "raster_url": raster_url,
                    "layer_id": layer_id,
                })
            except Exception as e:
                app.logger.exception(f"Error leyendo raster remoto {raster_url}: {e}")
                # Fall through to the local JRC + LUISA computation below.

        # --- Case B: local JRC + LUISA (default behaviour) -------------------
        ensure_data()

        d_jrc_max = 0.0
        d_jrc_avg = 0.0

        if JRC_PATH.exists():
            try:
                d_jrc_max, d_jrc_avg, _ = _stats_from_raster(JRC_PATH, geom_wgs84)
            except Exception as e:
                app.logger.exception(f"Error leyendo JRC: {e}")

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


@app.post("/query-grid")
def query_grid():
    """
    Densidad por celdas dentro de un bbox. Mucho más rápido que pedir celda a
    celda desde la Edge Function.

    Body: { "bbox": {"north":..,"south":..,"east":..,"west":..},
            "cell_size": 0.009, "raster_url": "<opcional>" }
    """
    try:
        data = request.get_json(force=True, silent=False) or {}
        bbox = data.get("bbox") or {}
        cell_size = float(data.get("cell_size") or 0.009)
        raster_url = data.get("raster_url")

        north = float(bbox["north"]); south = float(bbox["south"])
        east = float(bbox["east"]); west = float(bbox["west"])

        if cell_size < 0.001 or cell_size > 0.1:
            cell_size = 0.009

        if raster_url:
            source_path = _vsicurl(raster_url)
        else:
            ensure_data()
            if not JRC_PATH.exists():
                return jsonify({"error": "Raster JRC no disponible"}), 503
            source_path = str(JRC_PATH)

        MAX_CELLS = 20000
        cells = []

        with rasterio.open(source_path) as src:
            raster_crs = src.crs.to_string() if src.crs else "EPSG:4326"
            nodata = src.nodata

            lat = south
            while lat < north and len(cells) < MAX_CELLS:
                lng = west
                while lng < east and len(cells) < MAX_CELLS:
                    ring = [
                        [lng, lat],
                        [lng + cell_size, lat],
                        [lng + cell_size, lat + cell_size],
                        [lng, lat + cell_size],
                        [lng, lat],
                    ]
                    geom = mapping(Polygon(ring))
                    if raster_crs not in ("EPSG:4326", "WGS84"):
                        geom = transform_geom("EPSG:4326", raster_crs, geom, precision=6)
                    try:
                        out_image, _ = mask(src, [geom], crop=True, all_touched=True)
                        values = out_image[0].astype("float64")
                        m = np.isfinite(values) & (values > 0)
                        if nodata is not None:
                            m &= values != nodata
                        valid = values[m]
                        if valid.size > 0:
                            density = float(np.max(valid))
                            if density > 0:
                                cells.append({
                                    "lat": round(lat + cell_size / 2, 5),
                                    "lng": round(lng + cell_size / 2, 5),
                                    "density": round(density),
                                    "cellSize": cell_size,
                                })
                    except Exception:
                        pass
                    lng += cell_size
                lat += cell_size

        return jsonify({"cells": cells, "cell_size": cell_size})

    except Exception as e:
        app.logger.exception(f"Error en /query-grid: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), debug=True)
