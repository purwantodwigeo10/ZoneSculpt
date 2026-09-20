# ZoneSculpt

ZoneSculpt is a QGIS plugin for batch clipping georeferenced raster data with
polygon areas of interest (AOIs). Each AOI feature can create a separately
named output, or features can be merged by a selected attribute value.

## Features

- clip a raster with selected or all polygon AOI features;
- use an AOI attribute as the output filename;
- merge AOI features that share the same field value;
- create a master GeoTIFF for every clipping job;
- optionally export PNG, JPEG, JPEG2000, VRT, or MBTiles;
- validate source, boundary, and target coordinate reference systems;
- reproject output and handle LOCAL/unsupported raster CRS fallbacks;
- check raster and AOI overlap before processing;
- report progress, support cancellation, and retain completed outputs; and
- optionally add finished outputs to the current QGIS project.

Optional formats depend on the GDAL drivers available in the installed QGIS
build. A projected CRS is recommended when accurate spatial dimensions matter.

## Installation

1. Open **Plugins > Manage and Install Plugins > Install from ZIP** in QGIS.
2. Select the ZoneSculpt release ZIP and click **Install Plugin**.
3. Open **Raster > ZoneSculpt** or use the ZoneSculpt toolbar button.

## Quick test

1. Load a georeferenced raster in QGIS.
2. Load `sample_data/clip_boundary.geojson`, or use another polygon layer that
   overlaps the raster.
3. Select the raster and polygon layer in ZoneSculpt.
4. Choose `name` as the naming field.
5. Confirm the source and boundary CRS values.
6. Select an output directory and run the clip.
7. Verify that a GeoTIFF master and the output log are created.

The supplied boundary is synthetic and uses EPSG:4326. Move or replace it when
testing a raster located elsewhere.

## Activation and privacy

ZoneSculpt includes five successful trial runs per device. A trial is consumed
only after an export batch completes successfully. Activation requests and
license-status checks use the RUANG SPASIAL License Hub over HTTPS.

Only product identification, the activation code, Device ID, and computer name
are transmitted for activation. Raster files, AOI geometry, attributes, file
paths, and output data are not uploaded.

## Source, issues, and support

- Source: <https://github.com/purwantodwigeo10/ZoneSculpt>
- Issues: <https://github.com/purwantodwigeo10/ZoneSculpt/issues>
- Activation request: <https://aktivasi.ruangspasial.my.id/request>
- Support: <ruangspasial@gmail.com>

Copyright (C) 2026 Dwi Purwanto / RuangSpasial. Licensed under
GPL-3.0-or-later; see `LICENSE`.

