# Changelog

## 26.01

- Clip rasters in batches using polygon AOI features or selected boundaries.
- Name or group outputs from an AOI attribute and create a GeoTIFF master for every job.
- Export optional PNG, JPEG, JPEG2000, VRT, or MBTiles copies and load results into QGIS.
- Validate CRS settings, support optional reprojection, and handle LOCAL or unsupported raster CRS fallback.
- Apply the selected Target CRS to every non-MBTiles export, retain Source CRS when transformation is disabled, and verify the CRS written to each result.
- Create standards-compatible EPSG:3857 MBTiles from the transformed master raster.
- Add tiled GeoPackage output for users who need a tile database in the selected Target CRS.
- Write portable projection, band-role, and raster-statistics metadata for more consistent display in ArcMap and other GIS software.
- Preserve the source pixel grid and carry the complete source display style into current and reopened QGIS projects.
- Show cancellable 0–100 percent progress from validation through completed output creation.
