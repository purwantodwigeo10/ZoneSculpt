# ZoneSculpt

ZoneSculpt is a QGIS raster tool for clipping one georeferenced raster into
separate outputs using polygon AOI features. Each selected polygon, or each
group of polygons sharing an attribute value, can produce its own named
output.

## Requirements

- QGIS 3.22 through QGIS 3.99.
- A georeferenced raster supported by the GDAL build included with QGIS.
- A polygon or multipolygon AOI layer with a field suitable for output names.

## Main workflow

1. Load the source raster and polygon AOI layer in QGIS, or select them from files.
2. Choose the AOI naming field and whether to use selected features only.
3. Optionally merge AOI features that have the same field value.
4. Confirm the source, boundary, and optional target CRS.
5. Choose an output folder, filename prefix, and output format.
6. Click **Run Clip** and follow the 0–100 percent progress indicator.

ZoneSculpt always creates a GeoTIFF master for a completed clip job. It can
also export PNG, JPEG, JPEG2000, VRT, tiled GeoPackage, or MBTiles when the
corresponding GDAL driver is available. Completed outputs may be added directly to the current
QGIS project. When **Add Output to Project** is enabled, the result inherits
the complete source layer style, including its renderer, stretch, brightness,
contrast, gamma, opacity, and blend mode. ZoneSculpt also stores that output
style for later QGIS sessions and writes portable band, colour-role,
statistics, and projection metadata for other GIS software. Display appearance
can still vary because ArcMap, QGIS, and Global Mapper use different automatic
raster-stretch rules; the source pixel values remain unchanged.

## CRS handling

The plugin validates the raster, boundary, and target CRS before processing.
When a raster is reported as LOCAL or unsupported, the selected boundary CRS
can be used as the source-coordinate assumption. When **Reproject Output** is
enabled, the GeoTIFF master and PNG, JPEG, JPEG2000, or VRT export use the
selected Target CRS. When it is disabled, the output keeps the Source CRS even
when the boundary uses another CRS. MBTiles is always written in EPSG:3857, as
required for standard web-map tile compatibility. Reprojection is performed
on the pixels during clipping rather than merely changing the CRS label. The
**Tiled GeoPackage (.gpkg)** option is provided when a tiled database must keep
the selected Target CRS instead of Web Mercator. The
overlap preflight remains non-blocking for sources whose extent cannot be
reliably interpreted before GDAL runs. Clipping without a CRS change also
retains the native source pixel size and aligns the output grid to that
resolution.

## Trial and activation

Five successful clipping batches are available in trial mode. A successful
batch is counted once, regardless of the number of AOI outputs in that batch.
Use **Manage Activation** in the plugin window when continued use requires
activation.

## Installation

1. Open **Plugins > Manage and Install Plugins** in QGIS.
2. Choose **Install from ZIP**.
3. Select the ZoneSculpt ZIP package and install it.
4. Open ZoneSculpt from the **Raster** menu or its toolbar button.

## Source and support

- Source: <https://github.com/purwantodwigeo10/ZoneSculpt>
- Issues: <https://github.com/purwantodwigeo10/ZoneSculpt/issues>

Copyright (C) 2026 Dwi Purwanto / Ruang Spasial. Licensed under
GPL-3.0-or-later; see `LICENSE`.
