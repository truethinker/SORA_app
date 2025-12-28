# GDAL Density Server (Railway + Docker)

Servidor HTTP (Flask) para consultar densidades dentro de un polígono usando rasters (JRC + LUISA).

## Endpoints

- `GET /health`
- `POST /query-density`
  - Body JSON: `{ "polygon": [[lng, lat], [lng, lat], ...] }`
  - Respuesta JSON: `d_popmax`, `d_avg`, `d_jrc_max`, `d_luisa_conservative`, `luisa_class`

## Importante sobre los .tif

No se recomienda **meter rasters grandes dentro del repo** ni dentro de la imagen Docker: el build puede ser lento, y el límite de tamaño puede fastidiarte.

Este proyecto soporta 2 modos:

### Modo A (recomendado): descargar en runtime
Sube los .tif a un storage (S3/GCS/Azure Blob/etc.) y configura variables en Railway:

- `JRC_URL`  -> URL directa al TIFF
- `LUISA_URL` -> URL directa al TIFF
- (opcional) `DATA_DIR` -> `/app/data`

El servidor descargará los archivos automáticamente al primer request.

### Modo B: bake into image (solo si es pequeño)
Descomenta en Dockerfile:

```dockerfile
# COPY data/ ./data/
```

y añade tus rasters dentro de `data/`.

## Deploy en Railway

1. Sube este repo a GitHub.
2. Railway → **New Project** → **Deploy from GitHub repo**
3. Railway detecta el **Dockerfile** automáticamente.
4. Railway → Settings → Networking → **Generate Domain**
5. Define env vars si usas Modo A: `JRC_URL`, `LUISA_URL`

Railway inyecta `$PORT` automáticamente: el `CMD` ya está preparado para usarlo.

## Prueba rápida

- `GET https://TU-DOMINIO/health`
- `POST https://TU-DOMINIO/query-density`

Ejemplo body:

```json
{
  "polygon": [
    [2.154007, 41.390205],
    [2.1552, 41.390205],
    [2.1552, 41.3910],
    [2.154007, 41.3910],
    [2.154007, 41.390205]
  ]
}
```

## Notas GIS (CRS)
El servidor asume que el polígono viene en **EPSG:4326 (lon/lat)** y reproyecta al CRS del raster antes de aplicar `mask`.
