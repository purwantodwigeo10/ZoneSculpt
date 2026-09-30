# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
"""ZoneSculpt QGIS plugin: batch raster clipping by polygon boundaries."""

import hashlib
import hmac
import json
import os
import platform
import re
import sqlite3
import tempfile
import unicodedata
import uuid
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

from qgis.PyQt.QtWidgets import (
    QAction,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QInputDialog,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)
from qgis.PyQt.QtGui import QDesktopServices, QIcon, QPixmap
from qgis.PyQt.QtCore import Qt, QUrl, QSettings


# Qt enum compatibility helper for QGIS 3 (PyQt5) and QGIS 4 (PyQt6).
# PyQt5 exposes values as QT_WINDOW, QT_ALIGN_CENTER, etc.
# PyQt6 exposes many of them as QT_WINDOWType.Window, Qt.AlignmentFlag.AlignCenter, etc.
def _qt_enum(group_name, value_name):
    if hasattr(Qt, value_name):
        return getattr(Qt, value_name)
    group = getattr(Qt, group_name, None)
    if group is not None and hasattr(group, value_name):
        return getattr(group, value_name)
    raise AttributeError(f"Qt enum not found: {group_name}.{value_name}")

QT_RICH_TEXT = _qt_enum("TextFormat", "RichText")
QT_WINDOW = _qt_enum("WindowType", "Window")
QT_WINDOW_SYSTEM_MENU_HINT = _qt_enum("WindowType", "WindowSystemMenuHint")
QT_WINDOW_MINIMIZE_BUTTON_HINT = _qt_enum("WindowType", "WindowMinimizeButtonHint")
QT_WINDOW_MAXIMIZE_BUTTON_HINT = _qt_enum("WindowType", "WindowMaximizeButtonHint")
QT_WINDOW_CLOSE_BUTTON_HINT = _qt_enum("WindowType", "WindowCloseButtonHint")
QT_NON_MODAL = _qt_enum("WindowModality", "NonModal")
QT_ALIGN_CENTER = _qt_enum("AlignmentFlag", "AlignCenter")
QT_ALIGN_TOP = _qt_enum("AlignmentFlag", "AlignTop")
QT_ALIGN_RIGHT = _qt_enum("AlignmentFlag", "AlignRight")
QT_ALIGN_VCENTER = _qt_enum("AlignmentFlag", "AlignVCenter")
QT_KEEP_ASPECT_RATIO = _qt_enum("AspectRatioMode", "KeepAspectRatio")
QT_SMOOTH_TRANSFORMATION = _qt_enum("TransformationMode", "SmoothTransformation")


def _dialog_exec(dialog):
    # QGIS 3 / PyQt5 uses exec_(); QGIS 4 / PyQt6 uses exec().
    exec_method = getattr(dialog, "exec", None)
    if exec_method is None:
        exec_method = getattr(dialog, "exec_")
    return exec_method()

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsMapLayerStyle,
    QgsMapLayerProxyModel,
    QgsMessageLog,
    QgsProcessingFeedback,
    QgsProject,
    QgsRectangle,
    QgsRasterLayer,
    QgsVectorLayer,
    QgsVectorFileWriter,
    QgsWkbTypes,
    Qgis,
)
from qgis.gui import QgsFieldComboBox, QgsMapLayerComboBox, QgsProjectionSelectionWidget

import processing


def _qgis_enum(owner, group_name, value_name):
    """Resolve QGIS 3 unscoped and newer scoped enum values."""
    if hasattr(owner, value_name):
        return getattr(owner, value_name)
    group = getattr(owner, group_name, None)
    if group is not None and hasattr(group, value_name):
        return getattr(group, value_name)
    raise AttributeError(f"QGIS enum not found: {group_name}.{value_name}")


WKB_POLYGON_GEOMETRY = _qgis_enum(QgsWkbTypes, "GeometryType", "PolygonGeometry")
QGIS_WARNING = _qgis_enum(Qgis, "MessageLevel", "Warning")
QGIS_CRITICAL = _qgis_enum(Qgis, "MessageLevel", "Critical")
VECTOR_WRITER_NO_ERROR = _qgis_enum(QgsVectorFileWriter, "WriterError", "NoError")
LAYER_FILTER_RASTER = _qgis_enum(QgsMapLayerProxyModel, "Filter", "RasterLayer")
LAYER_FILTER_POLYGON = _qgis_enum(QgsMapLayerProxyModel, "Filter", "PolygonLayer")


PLUGIN_NAME = "ZoneSculpt"
PLUGIN_CODE = "ZS"
PLUGIN_VERSION = "26.01"
PLUGIN_COMPAT_MIN_QGIS = "3.22"
PLUGIN_COMPAT_MAX_QGIS = "3.99"

DEFAULT_ACTIVATION_SERVER_URL = "https://aktivasi.ruangspasial.my.id"
REQUEST_ACTIVATION_URL = "https://aktivasi.ruangspasial.my.id/request"


OUTPUT_FORMATS = [
    {
        "label": "GeoTIFF (.tif) - Master / Best Quality",
        "key": "gtiff",
        "ext": ".tif",
        "options": "BIGTIFF=IF_SAFER",
        "note": "Master GeoTIFF is always used for the clip step. MBTiles can be exported after the GeoTIFF is created.",
    },
    {
        "label": "PNG georeferenced (.png + world file)",
        "key": "png",
        "ext": ".png",
        "options": "WORLDFILE=YES",
        "note": "Optional display output. A master GeoTIFF is still created first.",
    },
    {
        "label": "JPEG georeferenced (.jpg + world file)",
        "key": "jpeg",
        "ext": ".jpg",
        "options": "WORLDFILE=YES|QUALITY=100",
        "note": "JPEG remains lossy even at maximum quality. A master GeoTIFF is still created first.",
    },
    {
        "label": "JPEG2000 (.jp2)",
        "key": "jp2",
        "ext": ".jp2",
        "options": "",
        "note": "Optional export. Write support depends on the JPEG2000 driver available in GDAL/QGIS.",
    },
    {
        "label": "VRT (.vrt)",
        "key": "vrt",
        "ext": ".vrt",
        "options": "",
        "note": "Optional virtual raster export. The master GeoTIFF must remain available.",
    },
    {
        "label": "Tiled GeoPackage (.gpkg) - Keeps Target CRS",
        "key": "gpkg",
        "ext": ".gpkg",
        "options": "TILE_FORMAT=PNG",
        "note": "Tiled raster database that keeps the selected Target CRS. Use this when MBTiles EPSG:3857 is not suitable.",
    },
]
COMMON_INPUT_RASTER_EXTS = {
    ".tif",
    ".tiff",
    ".mbtiles",
    ".ecw",
    ".png",
    ".jpg",
    ".jpeg",
    ".jp2",
    ".j2k",
    ".vrt",
    ".img",
    ".asc",
    ".bil",
    ".dem",
    ".gif",
    ".webp",
    ".gpkg",
}


class ActivationManager:
    """Simple per-device offline activation with 5 successful export trials."""

    TRIAL_LIMIT = 5
    SECRET_KEY = b"ZoneSculpt_DwiPurwanto_Activation_v1_ChangeThisSecretBeforeRelease"

    def __init__(self):
        self.settings = QSettings("DwiPurwanto", "ZoneSculptLicenseHubV2")
        # Paksa migrasi dari versi testing yang pernah menyimpan localhost/127.0.0.1.
        # Jika nilai lama dibiarkan, Windows akan menampilkan WinError 10061.
        saved_url = str(self.settings.value("activation_server_url", "") or "").strip()
        if (not saved_url) or ("127.0.0.1" in saved_url) or ("localhost" in saved_url) or (":8000" in saved_url) or (":8001" in saved_url):
            self.settings.setValue("activation_server_url", DEFAULT_ACTIVATION_SERVER_URL)
            self.settings.sync()

    def server_url(self):
        saved_url = str(self.settings.value("activation_server_url", DEFAULT_ACTIVATION_SERVER_URL) or DEFAULT_ACTIVATION_SERVER_URL).strip().rstrip("/")
        if (not saved_url) or ("127.0.0.1" in saved_url) or ("localhost" in saved_url) or (":8000" in saved_url) or (":8001" in saved_url):
            saved_url = DEFAULT_ACTIVATION_SERVER_URL
            self.settings.setValue("activation_server_url", saved_url)
            self.settings.sync()
        return saved_url

    def license_key(self):
        return str(self.settings.value("license_key", "") or "")

    def request_activation_url(self):
        params = urllib.parse.urlencode({
            "plugin_code": PLUGIN_CODE,
            "plugin": PLUGIN_NAME,
            "device_id": self.device_id(),
        })
        return f"{REQUEST_ACTIVATION_URL}?{params}"

    def clear_activation(self):
        for key in ["license_key", "activated_device", "activation_token", "activation_expires", "activation_code"]:
            try:
                self.settings.remove(key)
            except Exception:
                self.settings.setValue(key, "")
        self.settings.sync()

    def activate_with_server(self, server_url, license_key):
        server_url = (server_url or "").strip().rstrip("/")
        license_key = (license_key or "").strip().upper()
        if not server_url:
            return False, "Activation server URL is empty."
        if not license_key:
            return False, "License key is empty."

        endpoint = f"{server_url}/activate"
        payload = {
            "product": PLUGIN_NAME,
            "plugin_code": PLUGIN_CODE,
            "device_id": self.device_id(),
            "license_key": license_key,
            "machine_name": platform.node(),
        }
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read().decode("utf-8", errors="replace")
                result = json.loads(raw or "{}")
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8", errors="replace")
                result = json.loads(raw or "{}")
                return False, result.get("message") or result.get("detail") or str(exc)
            except Exception:
                return False, f"Activation server returned an error: {exc}"
        except Exception as exc:
            return False, f"Tidak bisa terhubung ke server aktivasi ({endpoint}). Detail: {exc}"

        if not result.get("ok"):
            return False, result.get("message", "Activation was rejected by the server.")

        self.settings.setValue("activation_server_url", server_url)
        self.settings.setValue("license_key", license_key)
        self.settings.setValue("activated_device", self.device_id())
        self.settings.setValue("activation_token", result.get("activation_token", "SERVER_OK"))
        self.settings.setValue("activation_expires", result.get("expires", "PERMANENT"))
        self.settings.sync()
        return True, result.get("message", "Activation successful.")

    def verify_activation_with_server(self):
        license_key = self.license_key().strip().upper()
        if not license_key:
            self.clear_activation()
            return False, "Kode aktivasi belum tersimpan. Silakan aktivasi ulang."

        if str(self.settings.value("activated_device", "") or "") != self.device_id():
            self.clear_activation()
            return False, "Aktivasi bukan untuk perangkat ini. Silakan ajukan request aktivasi baru."

        server_url = self.server_url().strip().rstrip("/")
        endpoint = f"{server_url}/activate"
        payload = {
            "product": PLUGIN_NAME,
            "plugin_code": PLUGIN_CODE,
            "device_id": self.device_id(),
            "license_key": license_key,
            "machine_name": platform.node(),
            "check_only": True,
        }
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                raw = response.read().decode("utf-8", errors="replace")
                result = json.loads(raw or "{}")
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8", errors="replace")
                result = json.loads(raw or "{}")
                msg = result.get("message") or result.get("detail") or str(exc)
            except Exception:
                msg = f"Server menolak lisensi: {exc}"
            self.clear_activation()
            return False, msg
        except Exception as exc:
            return False, f"Tidak bisa mengecek status lisensi ke server. Sambungkan internet lalu coba lagi. Detail: {exc}"

        if not result.get("ok"):
            self.clear_activation()
            return False, result.get("message", "Lisensi sudah tidak aktif atau tidak valid.")

        self.settings.setValue("activation_token", result.get("activation_token", str(self.settings.value("activation_token", "SERVER_OK") or "SERVER_OK")))
        self.settings.setValue("activation_expires", result.get("expires", str(self.settings.value("activation_expires", "PERMANENT") or "PERMANENT")))
        self.settings.setValue("activated_device", self.device_id())
        self.settings.setValue("license_key", license_key)
        self.settings.sync()
        return True, result.get("message", "Lisensi aktif.")

    def ensure_can_run(self):
        if self.is_activated():
            return self.verify_activation_with_server()
        if self.trial_remaining() > 0:
            return True, f"Trial tersisa {self.trial_remaining()} dari {self.TRIAL_LIMIT}."
        return False, "Trial 5 kali penggunaan sudah habis. Silakan ajukan aktivasi di website RUANG SPASIAL, lalu masukkan kode aktivasi."

    def device_id(self):
        parts = [platform.system(), platform.node(), str(uuid.getnode())]
        machine_guid = self._windows_machine_guid()
        if machine_guid:
            parts.append(machine_guid)
        raw = "|".join(str(part) for part in parts if part)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest().upper()[:32]

    def _windows_machine_guid(self):
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography")
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
            return value
        except Exception:
            return ""

    def trial_used(self):
        try:
            return int(self.settings.value("trial_used", 0))
        except Exception:
            return 0

    def trial_remaining(self):
        return max(0, self.TRIAL_LIMIT - self.trial_used())

    def is_activated(self):
        # Hanya license dari RUANG SPASIAL License Hub yang dianggap aktif.
        # Kode aktivasi offline/legacy sengaja tidak dipakai supaya status tidak salah "Active".
        license_key = self.license_key().strip().upper()
        token = str(self.settings.value("activation_token", "") or "")
        activated_device = str(self.settings.value("activated_device", "") or "")
        return bool(license_key and token and activated_device == self.device_id())

    def can_run(self):
        return self.is_activated() or self.trial_remaining() > 0

    def status_text(self):
        if self.is_activated():
            return "Active"
        if self.trial_remaining() <= 0:
            return "Belum Aktif - Trial Habis"
        return "Belum Aktif"

    def status_color(self):
        if self.is_activated():
            return "#0a7a33"
        return "#c62828"

    def record_successful_export(self):
        if self.is_activated():
            return
        used = min(self.TRIAL_LIMIT, self.trial_used() + 1)
        self.settings.setValue("trial_used", used)
        self.settings.sync()

    def activation_payload(self, device_id=None):
        device_id = (device_id or self.device_id()).strip().upper()
        return f"ZS|{device_id}|PERMANENT|V1"

    def expected_signature(self, device_id=None):
        payload = self.activation_payload(device_id)
        return hmac.new(self.SECRET_KEY, payload.encode("utf-8"), hashlib.sha256).hexdigest().upper()[:16]

    def format_code(self, device_id=None):
        device_id = (device_id or self.device_id()).strip().upper().replace("-", "")
        signature = self.expected_signature(device_id)
        raw = f"{device_id}{signature}"
        groups = [raw[i:i + 4] for i in range(0, len(raw), 4)]
        return "ZS-" + "-".join(groups)

    def verify_code(self, code):
        cleaned = re.sub(r"[^A-Za-z0-9]", "", str(code or "")).upper()
        if cleaned.startswith("ZS"):
            cleaned = cleaned[2:]
        if len(cleaned) != 32:
            return False
        code_device = cleaned[:16]
        code_sig = cleaned[16:]
        if code_device != self.device_id():
            return False
        return hmac.compare_digest(code_sig, self.expected_signature(code_device))

    def activate(self, code):
        if not self.verify_code(code):
            return False
        self.settings.setValue("activation_code", code.strip().upper())
        self.settings.setValue("activated_device", self.device_id())
        self.settings.sync()
        return True


class ActivationDialog(QDialog):
    def __init__(self, activation_manager, parent=None):
        super().__init__(parent)
        self.activation_manager = activation_manager
        self.setWindowTitle("Aktivasi ZoneSculpt")
        self.setMinimumWidth(700)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.status_label = QLabel("")
        self.status_label.setTextFormat(QT_RICH_TEXT)
        form.addRow("Status Aktivasi", self.status_label)

        device_row = QWidget()
        device_layout = QHBoxLayout(device_row)
        device_layout.setContentsMargins(0, 0, 0, 0)
        self.device_id_edit = QLineEdit(self.activation_manager.device_id())
        self.device_id_edit.setReadOnly(True)
        self.device_id_edit.setMinimumWidth(360)
        self.copy_device_button = QPushButton("Salin Device ID")
        self.copy_device_button.clicked.connect(self._copy_device_id)
        device_layout.addWidget(self.device_id_edit)
        device_layout.addWidget(self.copy_device_button)
        form.addRow("Device ID", device_row)

        self.trial_label = QLabel("")
        form.addRow("Pemakaian Trial", self.trial_label)

        self.code_edit = QLineEdit()
        self.code_edit.setPlaceholderText("Masukkan kode aktivasi dari License Hub")
        form.addRow("Kode Aktivasi", self.code_edit)
        layout.addLayout(form)

        button_row = QHBoxLayout()
        button_row.addStretch()
        self.request_button = QPushButton("Ajukan Request")
        self.request_button.clicked.connect(self._open_request_page)
        self.activate_button = QPushButton("Aktifkan")
        self.activate_button.clicked.connect(self._activate)
        self.close_button = QPushButton("Tutup")
        self.close_button.clicked.connect(self.accept)
        button_row.addWidget(self.request_button)
        button_row.addWidget(self.activate_button)
        button_row.addWidget(self.close_button)
        layout.addLayout(button_row)

        self._refresh()

    def _copy_device_id(self):
        QApplication.clipboard().setText(self.device_id_edit.text())
        QMessageBox.information(self, PLUGIN_NAME, "Device ID berhasil disalin ke clipboard.")

    def _open_request_page(self):
        QApplication.clipboard().setText(self.activation_manager.device_id())
        QDesktopServices.openUrl(QUrl(self.activation_manager.request_activation_url()))
        QMessageBox.information(
            self,
            PLUGIN_NAME,
            "Halaman request aktivasi sudah dibuka. Device ID juga sudah disalin ke clipboard.",
        )

    def _activate(self):
        code = self.code_edit.text().strip()
        if not code:
            QMessageBox.warning(self, PLUGIN_NAME, "Masukkan kode aktivasi terlebih dahulu.")
            return

        ok, message = self.activation_manager.activate_with_server(self.activation_manager.server_url(), code)
        if ok:
            QMessageBox.information(self, PLUGIN_NAME, message or "Aktivasi berhasil. ZoneSculpt sudah aktif pada perangkat ini.")
            self._refresh()
            self.accept()
            return

        QMessageBox.critical(self, PLUGIN_NAME, message or "Aktivasi gagal. Pastikan kode aktivasi, plugin code, dan Device ID cocok.")

    def _refresh(self):
        color = self.activation_manager.status_color()
        self.status_label.setText(f"<span style='font-weight:700; color:{color};'>{self.activation_manager.status_text()}</span>")
        used = self.activation_manager.trial_used()
        remaining = self.activation_manager.trial_remaining()
        self.trial_label.setText(f"{used}/{self.activation_manager.TRIAL_LIMIT} dipakai, {remaining} tersisa")


class ZoneSculptDialog(QDialog):
    def __init__(self, plugin_dir, activation_manager, parent=None):
        super().__init__(parent)
        self.plugin_dir = plugin_dir
        self.activation_manager = activation_manager
        self.setWindowTitle("ZoneSculpt")
        self.setWindowFlags(
            self.windowFlags()
            | QT_WINDOW
            | QT_WINDOW_SYSTEM_MENU_HINT
            | QT_WINDOW_MINIMIZE_BUTTON_HINT
            | QT_WINDOW_MAXIMIZE_BUTTON_HINT
            | QT_WINDOW_CLOSE_BUTTON_HINT
        )
        self.setWindowModality(QT_NON_MODAL)
        self.setModal(False)
        self.setSizeGripEnabled(True)
        self.setMinimumSize(720, 480)
        self.resize(860, 600)
        self.is_running = False
        self.cancel_requested = False
        self.current_feedback = None

        icon_path = os.path.join(self.plugin_dir, "icon.png")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        self.header_frame = QFrame()
        self.header_frame.setObjectName("zsHeaderFrame")
        self.header_frame.setStyleSheet("#zsHeaderFrame {background-color: #efefef; border: 1px solid #d0d0d0; border-radius: 4px;}")
        header_layout = QHBoxLayout(self.header_frame)
        header_layout.setContentsMargins(12, 10, 12, 10)
        header_layout.setSpacing(12)

        self.logo_label = QLabel()
        self.logo_label.setFixedSize(300, 220)
        self.logo_label.setAlignment(QT_ALIGN_CENTER)
        if os.path.exists(icon_path):
            pixmap = QPixmap(icon_path)
            if not pixmap.isNull():
                self.logo_label.setPixmap(pixmap.scaled(272, 204, QT_KEEP_ASPECT_RATIO, QT_SMOOTH_TRANSFORMATION))
        header_layout.addWidget(self.logo_label, 0, QT_ALIGN_TOP)

        self.header_text_widget = QWidget()
        header_text_layout = QVBoxLayout(self.header_text_widget)
        header_text_layout.setContentsMargins(0, 0, 0, 0)
        header_text_layout.setSpacing(0)

        self.title_label = QLabel("<span style='font-size:22px; font-weight:700;'>ZoneSculpt</span>")
        self.title_label.setTextFormat(QT_RICH_TEXT)
        self.title_label.setStyleSheet("margin: 0px; padding: 0px;")
        header_text_layout.addWidget(self.title_label, 0, QT_ALIGN_TOP)

        self.subtitle_label = QLabel(
            "ZoneSculpt helps users perform batch raster and vector clipping based on polygon AOI quickly and efficiently. "
            "It supports various GIS data formats and simplifies output naming, file format selection, and CRS management."
        )
        self.subtitle_label.setWordWrap(True)
        self.subtitle_label.setStyleSheet("margin: 0px; padding: 0px;")
        header_text_layout.addWidget(self.subtitle_label, 0, QT_ALIGN_TOP)
        header_text_layout.addStretch()
        header_layout.addWidget(self.header_text_widget, 1, QT_ALIGN_TOP)

        right_box = QWidget()
        right_layout = QVBoxLayout(right_box)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)

        self.header_status_label = QLabel()
        self.header_status_label.setAlignment(QT_ALIGN_RIGHT | QT_ALIGN_VCENTER)
        self.header_status_label.setTextFormat(QT_RICH_TEXT)
        right_layout.addWidget(self.header_status_label)

        self.manage_button = QPushButton("Manage Activation")
        self.manage_button.clicked.connect(self._manage_activation)
        right_layout.addWidget(self.manage_button, 0, QT_ALIGN_RIGHT)
        header_layout.addWidget(right_box, 0, QT_ALIGN_TOP)

        layout.addWidget(self.header_frame)

        form = QFormLayout()

        self.raster_combo = QgsMapLayerComboBox()
        self.raster_combo.setFilters(LAYER_FILTER_RASTER)
        self.raster_file_button = QPushButton("Add from file...")
        self.raster_file_button.clicked.connect(self._browse_raster_file)
        form.addRow("Input Raster", self._row(self.raster_combo, self.raster_file_button))

        self.aoi_combo = QgsMapLayerComboBox()
        self.aoi_combo.setFilters(LAYER_FILTER_POLYGON)
        self.aoi_file_button = QPushButton("Add from file...")
        self.aoi_file_button.clicked.connect(self._browse_aoi_file)
        form.addRow("Clip Boundary", self._row(self.aoi_combo, self.aoi_file_button))

        self.field_combo = QgsFieldComboBox()
        self.field_combo.setLayer(self.aoi_combo.currentLayer())
        self.aoi_combo.layerChanged.connect(self.field_combo.setLayer)
        form.addRow("Naming Field", self.field_combo)

        self.source_crs_selector = QgsProjectionSelectionWidget()
        self.mask_crs_selector = QgsProjectionSelectionWidget()
        self.output_crs_selector = QgsProjectionSelectionWidget()
        form.addRow("Source CRS", self.source_crs_selector)
        form.addRow("Boundary CRS", self.mask_crs_selector)

        self.reproject_check = QCheckBox("Reproject Output")
        self.reproject_check.setChecked(False)
        form.addRow("Target CRS", self._row(self.reproject_check, self.output_crs_selector))

        self.output_format_combo = QComboBox()
        for item in OUTPUT_FORMATS:
            self.output_format_combo.addItem(item["label"], item)
        # GeoTIFF is the default to prevent MBTiles/same-as-input outputs unless another format is selected manually.
        self.output_format_combo.setCurrentIndex(0)
        self.output_format_combo.currentIndexChanged.connect(self._format_changed)
        form.addRow("Output Format", self.output_format_combo)


        out_row = QWidget()
        out_layout = QHBoxLayout(out_row)
        out_layout.setContentsMargins(0, 0, 0, 0)
        self.output_folder_edit = QLineEdit()
        self.output_folder_edit.setPlaceholderText("Select output directory...")
        self.output_folder_edit.textChanged.connect(self._output_folder_text_changed)
        self.browse_button = QPushButton("Browse")
        self.browse_button.clicked.connect(self._browse_output_folder)
        self.open_folder_button = QPushButton("Open Folder")
        self.open_folder_button.setEnabled(False)
        self.open_folder_button.clicked.connect(self._open_output_folder)
        out_layout.addWidget(self.output_folder_edit)
        out_layout.addWidget(self.browse_button)
        out_layout.addWidget(self.open_folder_button)
        form.addRow("Output Directory", out_row)

        self.prefix_edit = QLineEdit("ZoneSculpt_")
        form.addRow("File Prefix", self.prefix_edit)

        layout.addLayout(form)

        self.selected_only_check = QCheckBox("Use Selected Features Only")
        self.selected_only_check.setChecked(True)
        layout.addWidget(self.selected_only_check)

        self.group_by_field_check = QCheckBox("Merge Features by Field Value")
        self.group_by_field_check.setChecked(False)
        layout.addWidget(self.group_by_field_check)

        self.load_outputs_check = QCheckBox("Add Output to Project")
        self.load_outputs_check.setChecked(False)
        layout.addWidget(self.load_outputs_check)

        self.keep_master_check = QCheckBox("Keep Master GeoTIFF (.tif)")
        self.keep_master_check.setChecked(True)
        self.keep_master_check.setEnabled(False)
        layout.addWidget(self.keep_master_check)


        export_mbtiles_row = QWidget()
        export_mbtiles_layout = QHBoxLayout(export_mbtiles_row)
        export_mbtiles_layout.setContentsMargins(0, 0, 0, 0)
        self.export_mbtiles_check = QCheckBox("Also Export to MBTiles")
        self.mbtiles_note_label = QLabel(
            "MBTiles uses PNG tiles and EPSG:3857 for compatibility. Other outputs use the selected Target CRS."
        )
        self.mbtiles_note_label.setWordWrap(True)
        export_mbtiles_layout.addWidget(self.export_mbtiles_check)
        export_mbtiles_layout.addWidget(self.mbtiles_note_label)
        export_mbtiles_layout.addStretch()
        layout.addWidget(export_mbtiles_row)

        self.info_label = QLabel("")
        self.info_label.setWordWrap(True)
        layout.addWidget(self.info_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setFormat("%p%")
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        self.status_label = QLabel("Ready for Raster Clipping")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        button_row = QHBoxLayout()
        button_row.addStretch()
        self.run_button = QPushButton("Run Clip")
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._request_cancel)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self._close_or_minimize)
        button_row.addWidget(self.run_button)
        button_row.addWidget(self.cancel_button)
        button_row.addWidget(self.close_button)
        layout.addLayout(button_row)

        self.raster_combo.layerChanged.connect(self._sync_raster_crs)
        self.aoi_combo.layerChanged.connect(self._sync_aoi_crs)
        self._sync_raster_crs(self.raster_combo.currentLayer())
        self._sync_aoi_crs(self.aoi_combo.currentLayer())
        self._format_changed()
        self._set_header_status("Ready", "Background Mode: Enabled", "#0a7a33")

    def _set_header_status(self, state_text, extra_text, color):
        activation_color = self.activation_manager.status_color()
        activation_text = self.activation_manager.status_text()
        self.header_status_label.setText(
            f"<span style='font-weight:700; color:{color};'>Plugin Status : {state_text}</span> | "
            f"<span style='color:{color};'>{extra_text}</span><br>"
            f"<span style='font-weight:700; color:{activation_color};'>Activation : {activation_text}</span>"
        )

    def _manage_activation(self):
        dialog = ActivationDialog(self.activation_manager, self)
        _dialog_exec(dialog)
        self._set_header_status("Ready", "Background Mode: Enabled", "#0a7a33")

    def _request_cancel(self):
        if not self.is_running:
            return
        self.cancel_requested = True
        self.status_label.setText("Cancel requested. Waiting for the current operation to stop safely...")
        self._set_header_status("Cancelling", "Stopping process", "#c62828")
        self.cancel_button.setEnabled(False)
        if self.current_feedback is not None:
            try:
                self.current_feedback.cancel()
            except Exception:
                pass

    def _close_or_minimize(self):
        if self.is_running:
            self.status_label.setText("Process is still running in the background. The window has been minimized; do not close QGIS until it finishes.")
            self._set_header_status("Running", "Window Minimized", "#b36b00")
            self.showMinimized()
            return
        self.reject()

    def closeEvent(self, event):
        if self.is_running:
            self.status_label.setText("Process is still running in the background. The window has been minimized; do not close QGIS until it finishes.")
            self._set_header_status("Running", "Window Minimized", "#b36b00")
            self.showMinimized()
            event.ignore()
            return
        super().closeEvent(event)

    def _row(self, *widgets):
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        for widget in widgets:
            row_layout.addWidget(widget)
        return row

    def _browse_raster_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Spatial Raster",
            "",
            "Spatial Raster (*.tif *.tiff *.mbtiles *.ecw *.png *.jpg *.jpeg *.jp2 *.j2k *.vrt *.img *.asc *.bil *.dem *.gif *.webp *.gpkg);;All files (*.*)",
        )
        if not path:
            return
        layer_name = os.path.splitext(os.path.basename(path))[0]
        layer = QgsRasterLayer(path, layer_name)
        if not layer.isValid():
            QMessageBox.warning(
                self,
                PLUGIN_NAME,
                "The raster cannot be opened as a spatial layer. Make sure the file has georeferencing and that the required GDAL driver is available in QGIS.",
            )
            return
        QgsProject.instance().addMapLayer(layer)
        self.raster_combo.setLayer(layer)
        self._sync_raster_crs(layer)

    def _browse_aoi_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Polygon AOI",
            "",
            "Vector AOI (*.shp *.gpkg *.kml *.kmz *.geojson *.json *.gml *.tab *.dxf);;All files (*.*)",
        )
        if not path:
            return
        layer_name = os.path.splitext(os.path.basename(path))[0]
        layer = QgsVectorLayer(path, layer_name, "ogr")
        if not layer.isValid():
            QMessageBox.warning(
                self,
                PLUGIN_NAME,
                "The AOI cannot be opened. For GPKG/KML files with multiple layers, open the polygon layer in QGIS first, then select it in ZoneSculpt.",
            )
            return
        if QgsWkbTypes.geometryType(layer.wkbType()) != WKB_POLYGON_GEOMETRY:
            QMessageBox.warning(self, PLUGIN_NAME, "AOI must be a polygon or multipolygon layer.")
            return
        QgsProject.instance().addMapLayer(layer)
        self.aoi_combo.setLayer(layer)
        self.field_combo.setLayer(layer)
        self._sync_aoi_crs(layer)

    def _browse_output_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Output Directory")
        if folder:
            self._set_output_folder(folder)

    def _set_output_folder(self, folder):
        folder = os.path.normpath(folder)
        self.output_folder_edit.setText(folder)
        self.open_folder_button.setEnabled(os.path.isdir(folder))
        self.status_label.setText(f"Output directory selected: {folder}")

    def _output_folder_text_changed(self, text):
        self.open_folder_button.setEnabled(os.path.isdir(text.strip()))

    def _open_output_folder(self):
        folder = self.output_folder_edit.text().strip()
        if not folder:
            QMessageBox.information(self, PLUGIN_NAME, "Output directory has not been selected.")
            return
        if not os.path.isdir(folder):
            QMessageBox.warning(self, PLUGIN_NAME, f"Output directory was not found:\n{folder}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def _sync_raster_crs(self, layer):
        # Some ECW files are reported by QGIS/GDAL as LOCAL_CS / unsupported.
        # GDAL cannot transform from that pseudo CRS. In that case, fall back
        # to Boundary CRS or Project CRS so the user can still clip data whose
        # coordinates are already in the same projected system as the AOI.
        if layer and self._is_usable_crs(layer.crs()):
            self.source_crs_selector.setCrs(layer.crs())
            self.output_crs_selector.setCrs(layer.crs())
            return

        fallback = None
        current_aoi = self.aoi_combo.currentLayer() if hasattr(self, "aoi_combo") else None
        if current_aoi and self._is_usable_crs(current_aoi.crs()):
            fallback = current_aoi.crs()
        elif QgsProject.instance().crs().isValid():
            fallback = QgsProject.instance().crs()

        if fallback and fallback.isValid():
            self.source_crs_selector.setCrs(fallback)
            self.output_crs_selector.setCrs(fallback)
            self.status_label.setText(
                "Input raster CRS is LOCAL/unsupported. Source CRS has been assumed from Boundary/Project CRS."
            ) if hasattr(self, "status_label") else None

    def _sync_aoi_crs(self, layer):
        if layer and self._is_usable_crs(layer.crs()):
            self.mask_crs_selector.setCrs(layer.crs())
            # If the raster CRS is LOCAL/unsupported, keep the source/target CRS aligned with the AOI.
            raster_layer = self.raster_combo.currentLayer() if hasattr(self, "raster_combo") else None
            if raster_layer and not self._is_usable_crs(raster_layer.crs()):
                self.source_crs_selector.setCrs(layer.crs())
                self.output_crs_selector.setCrs(layer.crs())
        elif QgsProject.instance().crs().isValid():
            self.mask_crs_selector.setCrs(QgsProject.instance().crs())

    def _is_usable_crs(self, crs):
        if not crs or not crs.isValid():
            return False
        text = " ".join([
            crs.authid() or "",
            crs.description() or "",
            crs.toWkt() or "",
        ]).lower()
        if "unsupported" in text or "local_cs" in text or "local -" in text:
            return False
        return bool(crs.authid() or crs.toWkt())

    def _format_changed(self):
        item = self.output_format_combo.currentData()
        if not item:
            return
        self.info_label.setText(item.get("note", ""))


class ZoneSculptPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dialog = None
        self.plugin_dir = os.path.dirname(__file__)
        self.activation_manager = ActivationManager()

    def _is_usable_crs(self, crs):
        if not crs or not crs.isValid():
            return False
        crs_text = " ".join([
            crs.authid() or "",
            crs.description() or "",
            crs.toWkt() or "",
        ]).lower()
        if "unsupported" in crs_text or "local_cs" in crs_text or "local -" in crs_text:
            return False
        return bool(crs.authid() or crs.toWkt())

    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, "icon.png")
        self.action = QAction(QIcon(icon_path), "ZoneSculpt", self.iface.mainWindow())
        self.action.setObjectName("ZoneSculptAction")
        self.action.setWhatsThis("Batch clip raster by AOI with per-device activation and 5 trial exports.")
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToRasterMenu("ZoneSculpt", self.action)

    def unload(self):
        if self.action:
            self.iface.removePluginRasterMenu("ZoneSculpt", self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action = None

    def run(self):
        self.dialog = ZoneSculptDialog(self.plugin_dir, self.activation_manager, self.iface.mainWindow())
        self.dialog.run_button.clicked.connect(self.execute_clip)
        self.dialog.show()

    def execute_clip(self, checked=False):
        dlg = self.dialog
        dlg.progress.setRange(0, 100)
        dlg.progress.setValue(0)
        dlg.status_label.setText("Starting: checking activation and input settings...")
        QApplication.processEvents()

        can_run, activation_message = self.activation_manager.ensure_can_run()
        if not can_run:
            QMessageBox.warning(
                dlg,
                PLUGIN_NAME,
                (activation_message or "Plugin belum aktif.")
                + "\n\nKlik OK untuk membuka dialog aktivasi/request."
            )
            dlg._manage_activation()
            return
        dlg.progress.setValue(5)
        QApplication.processEvents()

        raster = dlg.raster_combo.currentLayer()
        aoi = dlg.aoi_combo.currentLayer()
        field_name = dlg.field_combo.currentField()
        output_folder = dlg.output_folder_edit.text().strip()
        prefix = dlg.prefix_edit.text().strip()
        use_selected = dlg.selected_only_check.isChecked()
        group_by_field = dlg.group_by_field_check.isChecked()
        load_outputs = dlg.load_outputs_check.isChecked()
        reproject_output = dlg.reproject_check.isChecked()
        output_format = dlg.output_format_combo.currentData() or OUTPUT_FORMATS[0]
        export_mbtiles = dlg.export_mbtiles_check.isChecked()
        source_crs = dlg.source_crs_selector.crs()
        mask_crs = dlg.mask_crs_selector.crs()
        output_crs = dlg.output_crs_selector.crs()
        crs_warning = ""

        # Normalize CRS when raster reports LOCAL/unsupported CRS. This is common
        # for some ECW sources. We do not reproject from LOCAL_CS; instead we
        # assume the raster coordinates are already in the selected Boundary CRS.
        if raster is not None and mask_crs and dlg._is_usable_crs(mask_crs):
            if not dlg._is_usable_crs(source_crs):
                source_crs = mask_crs
                dlg.source_crs_selector.setCrs(mask_crs)
                crs_warning += "Source CRS was LOCAL/unsupported and was assumed from Boundary CRS.\n"
            if reproject_output and not dlg._is_usable_crs(output_crs):
                output_crs = mask_crs
                dlg.output_crs_selector.setCrs(mask_crs)
                crs_warning += "Target CRS was LOCAL/unsupported and was replaced with Boundary CRS.\n"

        if raster is None:
            QMessageBox.warning(dlg, PLUGIN_NAME, "Please select an input raster first.")
            return
        if aoi is None:
            QMessageBox.warning(dlg, PLUGIN_NAME, "Please select a clip boundary layer first.")
            return
        if QgsWkbTypes.geometryType(aoi.wkbType()) != WKB_POLYGON_GEOMETRY:
            QMessageBox.warning(dlg, PLUGIN_NAME, "The clip boundary layer must be polygon or multipolygon.")
            return
        if not field_name:
            QMessageBox.warning(dlg, PLUGIN_NAME, "Please select a naming field.")
            return
        if not output_folder:
            QMessageBox.warning(dlg, PLUGIN_NAME, "Please select an output directory first. The output directory field still appears empty.")
            return
        if not dlg._is_usable_crs(source_crs):
            QMessageBox.warning(
                dlg,
                PLUGIN_NAME,
                "Source CRS is LOCAL/unsupported. Please set Source CRS to the real raster CRS, usually the same CRS as the clip boundary."
            )
            return
        if not dlg._is_usable_crs(mask_crs):
            QMessageBox.warning(dlg, PLUGIN_NAME, "Boundary CRS is not valid. Please select the boundary coordinate reference system first.")
            return
        if reproject_output and not dlg._is_usable_crs(output_crs):
            QMessageBox.warning(dlg, PLUGIN_NAME, "Target CRS is LOCAL/unsupported. Please select a real target CRS or disable Reproject Output.")
            return

        # This CRS is authoritative for the GeoTIFF master and every optional
        # non-MBTiles export.  Earlier builds used Boundary CRS when
        # reprojection was disabled, which could silently change coordinates
        # whenever the raster and AOI used different CRSs.
        effective_output_crs = output_crs if reproject_output else source_crs
        if not dlg._is_usable_crs(effective_output_crs):
            QMessageBox.warning(dlg, PLUGIN_NAME, "The effective output CRS is invalid. Please check Source CRS and Target CRS.")
            return
        if not os.path.isdir(output_folder):
            try:
                os.makedirs(output_folder, exist_ok=True)
            except Exception as exc:
                QMessageBox.critical(dlg, PLUGIN_NAME, f"Output directory cannot be created:\n{exc}")
                return

        dlg.progress.setValue(10)
        dlg.status_label.setText("Reading polygon boundaries...")
        QApplication.processEvents()

        if aoi.selectedFeatureCount() > 0 and use_selected:
            features = list(aoi.selectedFeatures())
        else:
            features = list(aoi.getFeatures())

        if not features:
            QMessageBox.warning(dlg, PLUGIN_NAME, "The clip boundary layer has no features to process.")
            return

        dlg.progress.setValue(15)
        dlg.status_label.setText("Preparing clipping jobs and checking raster overlap...")
        QApplication.processEvents()

        # Non-blocking overlap check. Some ECW/LOCAL CRS rasters cannot be reliably
        # checked by QGIS before GDAL runs, even though clipping can still work.
        # We only log a warning and continue to the actual GDAL clip step.
        try:
            overlap_ok, overlap_message = self._preflight_overlap_check(raster, aoi, features, source_crs, mask_crs)
            if not overlap_ok:
                QgsMessageLog.logMessage(f"Preflight warning ignored: {overlap_message}", PLUGIN_NAME, QGIS_WARNING)
        except Exception as exc:
            QgsMessageLog.logMessage(f"Preflight check skipped: {exc}", PLUGIN_NAME, QGIS_WARNING)

        jobs = self._build_jobs(features, field_name, group_by_field)
        if not jobs:
            QMessageBox.warning(dlg, PLUGIN_NAME, "No clipping job could be created from the selected boundaries.")
            return
        used_names = set()
        outputs = []
        warnings = []
        master_options = "BIGTIFF=IF_SAFER"
        primary_ext = self._output_extension(output_format, raster)

        dlg.is_running = True
        dlg.cancel_requested = False
        dlg.current_feedback = None
        dlg.run_button.setEnabled(False)
        dlg.cancel_button.setEnabled(True)
        dlg.progress.setRange(0, 100)
        dlg.progress.setValue(20)
        dlg._set_header_status("Running", "Clip to GeoTIFF master, then export", "#b36b00")
        log_path = os.path.join(output_folder, "ZoneSculpt_output_log.txt")
        try:
            with open(log_path, "a", encoding="utf-8") as log_file:
                log_file.write("\n=== ZoneSculpt run started ===\n")
                log_file.write(f"Output folder: {output_folder}\n")
                log_file.write("Workflow: Clip to master GeoTIFF first, then optional export to MBTiles.\n")
                log_file.write(f"Primary output format: {output_format.get('label', '')}\n")
                log_file.write(f"Export MBTiles: {export_mbtiles}\n")
                if crs_warning:
                    log_file.write("CRS warning:\n" + crs_warning)
                log_file.write(f"Source CRS used: {source_crs.authid() or source_crs.description()}\n")
                log_file.write(f"Boundary CRS used: {mask_crs.authid() or mask_crs.description()}\n")
                log_file.write(f"Reproject output: {reproject_output}\n")
                log_file.write(f"Requested Target CRS: {(output_crs.authid() or output_crs.description()) if reproject_output else 'Not enabled'}\n")
                log_file.write(f"Effective GeoTIFF/export CRS: {effective_output_crs.authid() or effective_output_crs.description()}\n")
                if export_mbtiles:
                    log_file.write("Effective MBTiles CRS: EPSG:3857 (MBTiles standard)\n")
                log_file.write(f"Total jobs: {len(jobs)}\n")
        except Exception:
            pass

        if crs_warning:
            dlg.status_label.setText(crs_warning.strip())
            QApplication.processEvents()

        canceled = False

        try:
            with tempfile.TemporaryDirectory(prefix="zonesculpt_") as temp_dir:
                for idx, (raw_name, job_features) in enumerate(jobs, start=1):
                    if dlg.cancel_requested:
                        canceled = True
                        break

                    base_name = self._safe_filename(f"{prefix}{raw_name}")
                    master_path = self._unique_path(output_folder, base_name, used_names, ".tif")
                    used_names.add(os.path.basename(master_path).lower())
                    job_outputs = []

                    job_start_progress = 20 + int(((idx - 1) / len(jobs)) * 75)
                    job_end_progress = 20 + int((idx / len(jobs)) * 75)
                    master_progress = job_start_progress + max(
                        1, (job_end_progress - job_start_progress) // 2
                    )

                    dlg.status_label.setText(f"Processing {idx}/{len(jobs)}: creating master GeoTIFF {os.path.basename(master_path)}")
                    dlg.progress.setValue(job_start_progress)
                    QApplication.processEvents()

                    if dlg.cancel_requested:
                        canceled = True
                        break

                    mask_layer = self._make_mask_layer(aoi, job_features, mask_crs)
                    if not mask_layer or not mask_layer.isValid():
                        raise RuntimeError(f"Failed to create AOI mask for: {raw_name}")

                    # Create the master GeoTIFF directly in the output directory.
                    # Earlier versions used a temporary clip_*.tif when Reproject Output was enabled.
                    # On some Windows/QGIS setups GDAL did not create that temporary file, so the next
                    # warp step failed with "clip_1.tif not found". This version lets GDAL Warp clip
                    # and reproject in one step, writing directly to the final master file.
                    clip_output = master_path
                    clip_target_crs = effective_output_crs

                    clip_params = {
                        "INPUT": raster.source(),
                        "MASK": mask_layer,
                        "SOURCE_CRS": source_crs,
                        "TARGET_CRS": clip_target_crs,
                        "TARGET_EXTENT": None,
                        "NODATA": None,
                        "ALPHA_BAND": False,
                        "CROP_TO_CUTLINE": True,
                        "KEEP_RESOLUTION": True,
                        "SET_RESOLUTION": False,
                        "X_RESOLUTION": None,
                        "Y_RESOLUTION": None,
                        "MULTITHREADING": True,
                        "OPTIONS": master_options,
                        "DATA_TYPE": 0,
                        "EXTRA": "",
                        "OUTPUT": clip_output,
                    }

                    if not self._clip_raster_by_mask(dlg, raster.source(), mask_layer, master_path, source_crs, clip_target_crs, master_options, temp_dir):
                        canceled = True
                        self._cleanup_partial_outputs([master_path], outputs)
                        break

                    if not os.path.exists(master_path):
                        raise RuntimeError(f"Master GeoTIFF was not created: {master_path}")

                    self._prepare_cross_gis_raster(
                        master_path,
                        raster.source(),
                        effective_output_crs,
                    )
                    self._verify_raster_crs(master_path, effective_output_crs, "GeoTIFF master")

                    outputs.append(master_path)
                    job_outputs.append(master_path)
                    self._append_log(log_path, f"CREATED MASTER: {master_path}")
                    dlg.progress.setValue(min(master_progress, job_end_progress))
                    QApplication.processEvents()

                    primary_key = output_format.get("key")
                    if primary_key != "gtiff":
                        primary_path = self._unique_path(output_folder, base_name, used_names, primary_ext)
                        used_names.add(os.path.basename(primary_path).lower())
                        dlg.status_label.setText(f"Processing {idx}/{len(jobs)}: exporting {os.path.basename(primary_path)}")
                        QApplication.processEvents()
                        if not self._translate_raster_direct(dlg, master_path, primary_path, output_format.get("options", "")):
                            canceled = True
                            self._cleanup_partial_outputs([primary_path], outputs)
                            break
                        if not os.path.exists(primary_path):
                            raise RuntimeError(f"Optional output was not created: {primary_path}")
                        self._prepare_cross_gis_raster(
                            primary_path,
                            master_path,
                            effective_output_crs,
                        )
                        self._write_esri_prj_sidecar(primary_path, effective_output_crs)
                        self._verify_raster_crs(primary_path, effective_output_crs, "optional export")
                        outputs.append(primary_path)
                        job_outputs.append(primary_path)
                        self._append_log(log_path, f"CREATED EXPORT: {primary_path}")

                    if export_mbtiles:
                        if not self._gdal_driver_available("MBTiles"):
                            msg = "MBTiles export skipped: MBTiles driver is not available in this QGIS/GDAL installation."
                            if msg not in warnings:
                                warnings.append(msg)
                            self._append_log(log_path, f"SKIPPED MBTILES: {msg}")
                        else:
                            mercator_path = os.path.join(temp_dir, f"mbtiles_3857_{idx}.tif")
                            mbtiles_path = self._unique_path(output_folder, base_name, used_names, ".mbtiles")
                            used_names.add(os.path.basename(mbtiles_path).lower())
                            dlg.status_label.setText(f"Processing {idx}/{len(jobs)}: preparing EPSG:3857 for MBTiles")
                            QApplication.processEvents()
                            mb_warp_params = {
                                "INPUT": master_path,
                                "SOURCE_CRS": effective_output_crs,
                                "TARGET_CRS": QgsCoordinateReferenceSystem("EPSG:3857"),
                                "RESAMPLING": 0,
                                "NODATA": None,
                                "TARGET_RESOLUTION": None,
                                "OPTIONS": master_options,
                                "DATA_TYPE": 0,
                                "TARGET_EXTENT": None,
                                "TARGET_EXTENT_CRS": None,
                                "MULTITHREADING": True,
                                "EXTRA": "",
                                "OUTPUT": mercator_path,
                            }
                            mbtiles_crs = QgsCoordinateReferenceSystem("EPSG:3857")
                            if not self._warp_reproject_direct(dlg, master_path, mercator_path, effective_output_crs, mbtiles_crs, master_options):
                                canceled = True
                                self._cleanup_partial_outputs([mercator_path, mbtiles_path], outputs)
                                break
                            self._prepare_cross_gis_raster(
                                mercator_path,
                                master_path,
                                mbtiles_crs,
                            )
                            self._verify_raster_crs(mercator_path, mbtiles_crs, "MBTiles preparation")
                            dlg.status_label.setText(f"Processing {idx}/{len(jobs)}: exporting MBTiles {os.path.basename(mbtiles_path)}")
                            QApplication.processEvents()
                            if dlg.cancel_requested:
                                canceled = True
                                self._cleanup_partial_outputs([mbtiles_path], outputs)
                                break
                            self._export_mbtiles_direct(mercator_path, mbtiles_path, tile_format="PNG", name=base_name)
                            if not os.path.exists(mbtiles_path):
                                raise RuntimeError(f"MBTiles export was not created: {mbtiles_path}")
                            if not self._is_valid_mbtiles(mbtiles_path):
                                raise RuntimeError(f"MBTiles export was created but is not a valid MBTiles database: {mbtiles_path}")
                            self._verify_raster_crs(mbtiles_path, mbtiles_crs, "MBTiles")
                            outputs.append(mbtiles_path)
                            job_outputs.append(mbtiles_path)
                            self._append_log(log_path, f"CREATED MBTILES: {mbtiles_path}")

                    if load_outputs:
                        for path in job_outputs:
                            self._add_output_layer(path, raster)

                    dlg.progress.setValue(min(95, job_end_progress))
                    QApplication.processEvents()

            if canceled:
                self._append_log(log_path, f"CANCELLED: {len(outputs)} completed output file(s).")
                dlg.status_label.setText(f"Cancelled. {len(outputs)} completed output file(s) remain in: {output_folder}")
                dlg._set_header_status("Cancelled", f"Outputs kept: {len(outputs)} file", "#c62828")
                self.iface.messageBar().pushWarning(PLUGIN_NAME, f"Process cancelled. {len(outputs)} completed output file(s) were kept.")
                QMessageBox.information(
                    dlg,
                    PLUGIN_NAME,
                    f"Process cancelled.\n\nCompleted output file(s) kept: {len(outputs)}\nOutput directory:\n{output_folder}",
                )
                return

            if warnings:
                for warning in warnings:
                    self.iface.messageBar().pushWarning(PLUGIN_NAME, warning)
                    self._append_log(log_path, f"WARNING: {warning}")

            dlg.progress.setValue(97)
            dlg.status_label.setText("Finalizing completed outputs...")
            QApplication.processEvents()
            self.activation_manager.record_successful_export()
            dlg.progress.setValue(100)
            dlg.status_label.setText(f"Completed. {len(outputs)} file(s) were created in: {output_folder}")
            dlg._set_header_status("Completed", f"Outputs: {len(outputs)} file", "#0a7a33")
            self.iface.messageBar().pushSuccess(PLUGIN_NAME, f"Created {len(outputs)} output file(s).")
            warning_text = ""
            if warnings:
                warning_text = "\n\nWarning(s):\n- " + "\n- ".join(warnings)
            QMessageBox.information(dlg, PLUGIN_NAME, f"Created {len(outputs)} output file(s).\n\nOutput directory:\n{output_folder}{warning_text}")
        except Exception as exc:
            dlg._set_header_status("Error", "Check message details", "#c62828")
            QgsMessageLog.logMessage(str(exc), PLUGIN_NAME, QGIS_CRITICAL)
            self.iface.messageBar().pushCritical(PLUGIN_NAME, f"Failed: {exc}")
            QMessageBox.critical(dlg, PLUGIN_NAME, f"Process failed:\n{exc}")
        finally:
            dlg.is_running = False
            dlg.current_feedback = None
            dlg.run_button.setEnabled(True)
            dlg.cancel_button.setEnabled(False)
            if (
                "Completed" not in dlg.header_status_label.text()
                and "Error" not in dlg.header_status_label.text()
                and "Cancelled" not in dlg.header_status_label.text()
            ):
                dlg._set_header_status("Ready", "Background Mode: Enabled", "#0a7a33")

    def _source_path(self, source):
        return str(source).split("|")[0]

    def _creation_options_list(self, options):
        if not options:
            return []
        return [part for part in str(options).split("|") if part.strip()]

    def _crs_to_gdal_srs(self, crs):
        if crs and crs.isValid():
            authid = crs.authid()
            if authid:
                return authid
            return crs.toWkt()
        return None

    def _save_mask_layer(self, mask_layer, output_path):
        try:
            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName = "GPKG"
            options.layerName = "mask"
            options.fileEncoding = "UTF-8"
            result = QgsVectorFileWriter.writeAsVectorFormatV3(
                mask_layer,
                output_path,
                QgsProject.instance().transformContext(),
                options,
            )
            # QGIS versions return different tuple layouts. Error code is normally first item.
            error_code = result[0] if isinstance(result, tuple) else result
            if error_code == VECTOR_WRITER_NO_ERROR:
                return True, "mask"
        except Exception:
            pass

        # Fallback for older QGIS builds.
        try:
            result = QgsVectorFileWriter.writeAsVectorFormat(
                mask_layer,
                output_path,
                "UTF-8",
                mask_layer.crs(),
                "GPKG",
            )
            error_code = result[0] if isinstance(result, tuple) else result
            if error_code == VECTOR_WRITER_NO_ERROR:
                return True, "mask"
        except Exception as exc:
            raise RuntimeError(f"Failed to save temporary AOI mask for GDAL clipping: {exc}")

        raise RuntimeError("Failed to save temporary AOI mask for GDAL clipping.")

    def _clip_raster_by_mask(self, dlg, input_source, mask_layer, output_path, source_crs, target_crs, options, temp_dir):
        """Clip/reproject using GDAL Python directly with explicit cutline CRS and clearer diagnostics."""
        if dlg.cancel_requested:
            return False
        try:
            from osgeo import gdal
            gdal.UseExceptions()
        except Exception as exc:
            raise RuntimeError(
                "GDAL Python bindings are not available. Please use the official QGIS installer with GDAL support. "
                f"Details: {exc}"
            )

        mask_path = os.path.join(temp_dir, f"zonesculpt_mask_{uuid.uuid4().hex}.gpkg")
        _, layer_name = self._save_mask_layer(mask_layer, mask_path)
        input_path = self._source_path(input_source)
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except Exception:
                pass

        creation_options = self._creation_options_list(options)
        grid_options = self._native_grid_options(gdal, input_path, source_crs, target_crs)
        common_warp_options = {
            "format": "GTiff",
            "srcSRS": self._crs_to_gdal_srs(source_crs),
            "dstSRS": self._crs_to_gdal_srs(target_crs),
            "cutlineDSName": mask_path,
            "cutlineLayer": layer_name,
            "cropToCutline": True,
            "multithread": True,
            "creationOptions": creation_options,
            "resampleAlg": "near",
        }
        common_warp_options.update(grid_options)
        try:
            warp_options = gdal.WarpOptions(
                cutlineSRS=self._crs_to_gdal_srs(mask_layer.crs()),
                **common_warp_options,
            )
        except TypeError:
            # Older GDAL builds do not expose cutlineSRS in WarpOptions.
            warp_options = gdal.WarpOptions(**common_warp_options)

        try:
            result = gdal.Warp(output_path, input_path, options=warp_options)
        except Exception as exc:
            err = str(exc) or gdal.GetLastErrorMsg()
            raise RuntimeError(
                "GDAL failed to clip the raster.\n\n"
                f"GDAL detail: {err}\n\n"
                f"Input raster: {input_path}\n"
                f"Mask CRS: {mask_layer.crs().authid() or mask_layer.crs().description()}\n"
                f"Source CRS: {source_crs.authid() or source_crs.description()}\n"
                f"Target CRS: {target_crs.authid() or target_crs.description()}\n\n"
                "Most common causes: the selected AOI is outside the raster, the Source CRS/Boundary CRS is not the real CRS of the layer, or the raster is not readable by GDAL."
            )
        if result is None:
            err = gdal.GetLastErrorMsg()
            raise RuntimeError(
                "GDAL failed to clip the raster.\n\n"
                f"GDAL detail: {err}\n\n"
                f"Input raster: {input_path}\n"
                f"Mask CRS: {mask_layer.crs().authid() or mask_layer.crs().description()}\n"
                f"Source CRS: {source_crs.authid() or source_crs.description()}\n"
                f"Target CRS: {target_crs.authid() or target_crs.description()}\n\n"
                "Most common causes: the selected AOI is outside the raster, the Source CRS/Boundary CRS is not the real CRS of the layer, or the raster is not readable by GDAL."
            )
        result = None
        return not dlg.cancel_requested

    def _native_grid_options(self, gdal, input_path, source_crs, target_crs):
        """Keep the source pixel size/grid when clipping without reprojection."""
        if not source_crs or not target_crs or source_crs != target_crs:
            return {}

        dataset = None
        try:
            dataset = gdal.Open(input_path)
            if dataset is None:
                return {}
            transform = dataset.GetGeoTransform()
            if not transform or len(transform) < 6:
                return {}
            if abs(float(transform[2])) > 1e-12 or abs(float(transform[4])) > 1e-12:
                return {}
            pixel_width = abs(float(transform[1]))
            pixel_height = abs(float(transform[5]))
            if pixel_width <= 0 or pixel_height <= 0:
                return {}
            return {
                "xRes": pixel_width,
                "yRes": pixel_height,
                "targetAlignedPixels": True,
            }
        except Exception:
            return {}
        finally:
            dataset = None

    def _prepare_cross_gis_raster(self, output_path, reference_path, expected_crs):
        """Embed CRS, band roles and stable statistics for non-QGIS readers.

        QGIS stores renderer/stretch settings in a QML style.  ArcMap does not
        read that style and otherwise calculates a new stretch for every clip,
        which can make an unchanged image look pale.  The pixel values are not
        modified here; portable raster metadata is copied instead.
        """
        try:
            from osgeo import gdal

            output_ds = gdal.Open(output_path, gdal.GA_Update)
            if output_ds is None:
                QgsMessageLog.logMessage(
                    f"Cross-GIS metadata could not be written to: {output_path}",
                    PLUGIN_NAME,
                    QGIS_WARNING,
                )
                return

            expected_wkt = expected_crs.toWkt() if expected_crs and expected_crs.isValid() else ""
            if expected_wkt:
                output_ds.SetProjection(expected_wkt)
                output_ds.SetMetadataItem(
                    "ZONESCULPT_OUTPUT_CRS",
                    expected_crs.authid() or expected_crs.description(),
                )

            reference_ds = gdal.Open(self._source_path(reference_path))
            if reference_ds is not None:
                for band_number in range(1, min(output_ds.RasterCount, reference_ds.RasterCount) + 1):
                    source_band = reference_ds.GetRasterBand(band_number)
                    output_band = output_ds.GetRasterBand(band_number)
                    if source_band is None or output_band is None:
                        continue

                    try:
                        output_band.SetColorInterpretation(source_band.GetColorInterpretation())
                    except Exception:
                        pass

                    try:
                        color_table = source_band.GetColorTable()
                        if color_table is not None:
                            output_band.SetColorTable(color_table.Clone())
                    except Exception:
                        pass

                    try:
                        no_data = source_band.GetNoDataValue()
                        if no_data is not None:
                            output_band.SetNoDataValue(no_data)
                    except Exception:
                        pass

                    # Reuse existing source statistics when available.  Do not
                    # force a full scan of very large imagery during clipping.
                    try:
                        statistics = source_band.GetStatistics(True, False)
                        if (
                            statistics
                            and len(statistics) == 4
                            and statistics[0] is not None
                            and float(statistics[1]) > float(statistics[0])
                            and float(statistics[3]) >= 0
                        ):
                            output_band.SetStatistics(*statistics)
                    except Exception:
                        pass

                    for getter_name, setter_name in (
                        ("GetScale", "SetScale"),
                        ("GetOffset", "SetOffset"),
                        ("GetUnitType", "SetUnitType"),
                    ):
                        try:
                            value = getattr(source_band, getter_name)()
                            if value not in (None, ""):
                                getattr(output_band, setter_name)(value)
                        except Exception:
                            pass

                reference_ds = None

            output_ds.FlushCache()
            output_ds = None
        except Exception as exc:
            QgsMessageLog.logMessage(
                f"Cross-GIS raster metadata warning for {output_path}: {exc}",
                PLUGIN_NAME,
                QGIS_WARNING,
            )

    def _write_esri_prj_sidecar(self, output_path, crs):
        """Write an ESRI WKT sidecar for formats whose world file has no CRS."""
        if os.path.splitext(output_path)[1].lower() not in {".png", ".jpg", ".jpeg"}:
            return
        if not crs or not crs.isValid():
            return
        try:
            from osgeo import osr

            spatial_ref = osr.SpatialReference()
            spatial_ref.ImportFromWkt(crs.toWkt())
            spatial_ref.MorphToESRI()
            prj_path = os.path.splitext(output_path)[0] + ".prj"
            with open(prj_path, "w", encoding="utf-8") as prj_file:
                prj_file.write(spatial_ref.ExportToWkt())
        except Exception as exc:
            QgsMessageLog.logMessage(
                f"ESRI projection sidecar could not be created for {output_path}: {exc}",
                PLUGIN_NAME,
                QGIS_WARNING,
            )

    def _verify_raster_crs(self, raster_path, expected_crs, label):
        """Stop the run if an export was only relabelled or lost its CRS."""
        try:
            from osgeo import gdal, osr

            dataset = gdal.Open(raster_path)
            actual_wkt = dataset.GetProjectionRef() if dataset is not None else ""
            dataset = None
            if not actual_wkt:
                prj_path = os.path.splitext(raster_path)[0] + ".prj"
                if os.path.exists(prj_path):
                    with open(prj_path, "r", encoding="utf-8") as prj_file:
                        actual_wkt = prj_file.read()
            if not actual_wkt:
                raise RuntimeError(f"{label} has no readable coordinate reference system: {raster_path}")

            actual_ref = osr.SpatialReference()
            expected_ref = osr.SpatialReference()
            actual_ref.ImportFromWkt(actual_wkt)
            expected_ref.ImportFromWkt(expected_crs.toWkt())
            if not bool(actual_ref.IsSame(expected_ref)):
                actual_name = actual_ref.GetName() or "unknown"
                expected_name = expected_crs.authid() or expected_crs.description()
                raise RuntimeError(
                    f"{label} CRS verification failed. Expected {expected_name}, but the file reports {actual_name}."
                )
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Unable to verify {label} CRS: {exc}")

    def _add_output_layer(self, output_path, source_layer):
        """Load an output and retain the source raster's visible QGIS style."""
        layer_name = os.path.splitext(os.path.basename(output_path))[0]
        output_layer = QgsRasterLayer(output_path, layer_name)
        if not output_layer.isValid():
            return False

        # Add first, because project insertion may initialize a provider style.
        # The complete source style is deliberately applied afterwards.
        QgsProject.instance().addMapLayer(output_layer)
        style_applied = False
        try:
            if source_layer is not None:
                source_style = QgsMapLayerStyle()
                source_style.readFromLayer(source_layer)
                if source_style.isValid():
                    source_style.writeToLayer(output_layer)
                    style_applied = True
        except Exception as exc:
            QgsMessageLog.logMessage(
                f"Complete source style could not be copied to the output raster: {exc}",
                PLUGIN_NAME,
                QGIS_WARNING,
            )

        # Renderer cloning is retained as a QGIS-version-safe fallback and also
        # refreshes provider-dependent band links after applying the full style.
        try:
            source_renderer = source_layer.renderer() if source_layer else None
            if source_renderer is not None:
                output_layer.setRenderer(source_renderer.clone())
                style_applied = True
        except Exception as exc:
            QgsMessageLog.logMessage(
                f"Output renderer could not be copied from the source raster: {exc}",
                PLUGIN_NAME,
                QGIS_WARNING,
            )

        try:
            source_filter = source_layer.brightnessFilter() if source_layer else None
            output_filter = output_layer.brightnessFilter()
            if source_filter is not None and output_filter is not None:
                for getter_name, setter_name in (
                    ("brightness", "setBrightness"),
                    ("contrast", "setContrast"),
                    ("gamma", "setGamma"),
                ):
                    getter = getattr(source_filter, getter_name, None)
                    setter = getattr(output_filter, setter_name, None)
                    if callable(getter) and callable(setter):
                        setter(getter())
        except Exception as exc:
            QgsMessageLog.logMessage(
                f"Output brightness settings could not be copied: {exc}",
                PLUGIN_NAME,
                QGIS_WARNING,
            )

        try:
            if source_layer is not None:
                output_layer.setBlendMode(source_layer.blendMode())
        except Exception:
            pass

        # Store a sidecar/default QML style so reopening the output later does
        # not make QGIS calculate a different automatic stretch again.
        if style_applied:
            try:
                style_uri = output_layer.styleURI()
                if style_uri:
                    save_result = output_layer.saveNamedStyle(style_uri)
                    if isinstance(save_result, tuple) and len(save_result) > 1 and not save_result[1]:
                        QgsMessageLog.logMessage(
                            f"Output style sidecar could not be saved: {save_result[0]}",
                            PLUGIN_NAME,
                            QGIS_WARNING,
                        )
            except Exception as exc:
                QgsMessageLog.logMessage(
                    f"Output style sidecar could not be saved: {exc}",
                    PLUGIN_NAME,
                    QGIS_WARNING,
                )

        output_layer.triggerRepaint()
        try:
            self.iface.layerTreeView().refreshLayerSymbology(output_layer.id())
        except Exception:
            pass
        try:
            self.iface.mapCanvas().refresh()
        except Exception:
            pass
        return True

    def _translate_raster_direct(self, dlg, input_path, output_path, options):
        if dlg.cancel_requested:
            return False
        try:
            from osgeo import gdal
        except Exception as exc:
            raise RuntimeError(f"GDAL Python bindings are not available for raster export: {exc}")
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except Exception:
                pass
        src = gdal.Open(input_path)
        if src is None:
            raise RuntimeError(f"Cannot open raster for export: {input_path}")
        result = gdal.Translate(
            output_path,
            src,
            creationOptions=self._creation_options_list(options),
        )
        src = None
        if result is None:
            raise RuntimeError(f"GDAL failed to export raster: {output_path}")
        result = None
        return not dlg.cancel_requested

    def _warp_reproject_direct(self, dlg, input_path, output_path, source_crs, target_crs, options):
        if dlg.cancel_requested:
            return False
        try:
            from osgeo import gdal
        except Exception as exc:
            raise RuntimeError(f"GDAL Python bindings are not available for reprojection: {exc}")
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except Exception:
                pass
        warp_options = gdal.WarpOptions(
            format="GTiff",
            srcSRS=self._crs_to_gdal_srs(source_crs),
            dstSRS=self._crs_to_gdal_srs(target_crs),
            multithread=True,
            creationOptions=self._creation_options_list(options),
            resampleAlg="near",
        )
        result = gdal.Warp(output_path, input_path, options=warp_options)
        if result is None:
            raise RuntimeError(f"GDAL failed to reproject raster: {output_path}")
        result = None
        return not dlg.cancel_requested

    def _run_algorithm(self, dlg, algorithm_id, params):
        feedback = QgsProcessingFeedback()
        dlg.current_feedback = feedback
        try:
            processing.run(algorithm_id, params, feedback=feedback)
        except Exception as exc:
            if dlg.cancel_requested or feedback.isCanceled():
                return False
            if "not found" in str(exc).lower() and algorithm_id.startswith("gdal:"):
                raise RuntimeError(
                    f"QGIS Processing GDAL algorithm was not found: {algorithm_id}. "
                    "ZoneSculpt uses direct GDAL for the main clip/export workflow, but this optional step still needs QGIS GDAL Processing. "
                    "Enable Processing > GDAL provider in QGIS, or reinstall QGIS with GDAL support."
                )
            raise
        finally:
            dlg.current_feedback = None
        if dlg.cancel_requested or feedback.isCanceled():
            return False
        return True

    def _translate_params(self, input_path, output_path, options):
        return {
            "INPUT": input_path,
            "TARGET_CRS": None,
            "NODATA": None,
            "COPY_SUBDATASETS": False,
            "OPTIONS": options or "",
            "EXTRA": "",
            "DATA_TYPE": 0,
            "OUTPUT": output_path,
        }

    def _gdal_driver_available(self, driver_name):
        try:
            from osgeo import gdal
            driver = gdal.GetDriverByName(driver_name)
            if driver is None:
                return False
            metadata = driver.GetMetadata()
            return bool(metadata.get("DCAP_CREATE") or metadata.get("DCAP_CREATECOPY"))
        except Exception:
            return False

    def _export_mbtiles_direct(self, input_path, output_path, tile_format="PNG", name="ZoneSculpt"):
        """Export a prepared EPSG:3857 GeoTIFF to a standards-friendly MBTiles file."""
        if not output_path.lower().endswith(".mbtiles"):
            output_path = f"{output_path}.mbtiles"
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except Exception:
                pass
        try:
            from osgeo import gdal
        except Exception as exc:
            raise RuntimeError(f"GDAL Python bindings are not available for MBTiles export: {exc}")

        src = gdal.Open(input_path)
        if src is None:
            raise RuntimeError(f"Cannot open prepared raster for MBTiles export: {input_path}")

        creation_options = [f"TILE_FORMAT={tile_format.upper()}"]
        if tile_format.upper() == "JPEG":
            creation_options.append("QUALITY=95")

        result = gdal.Translate(
            output_path,
            src,
            format="MBTILES",
            creationOptions=creation_options,
        )
        src = None
        if result is None:
            raise RuntimeError("GDAL failed to create MBTiles output.")
        result = None
        self._ensure_mbtiles_metadata(output_path, name=name, tile_format=tile_format)
        return output_path

    def _ensure_mbtiles_metadata(self, mbtiles_path, name="ZoneSculpt", tile_format="PNG"):
        """Make sure common MBTiles metadata exists for better compatibility in GIS software."""
        fmt = tile_format.lower()
        with sqlite3.connect(mbtiles_path) as conn:
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS metadata (name TEXT, value TEXT)")
            existing = {row[0] for row in cur.execute("SELECT name FROM metadata")}
            values = {
                "name": name,
                "type": "overlay",
                "version": "1.1",
                "description": "Created by ZoneSculpt",
                "format": fmt,
                "crs": "EPSG:3857",
            }
            for key, value in values.items():
                if key in existing:
                    cur.execute("UPDATE metadata SET value=? WHERE name=?", (value, key))
                else:
                    cur.execute("INSERT INTO metadata (name, value) VALUES (?, ?)", (key, value))
            conn.commit()

    def _is_valid_mbtiles(self, mbtiles_path):
        try:
            if not mbtiles_path.lower().endswith(".mbtiles"):
                return False
            with sqlite3.connect(mbtiles_path) as conn:
                cur = conn.cursor()
                tables = {row[0].lower() for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "tiles" not in tables or "metadata" not in tables:
                    return False
                tile_count = cur.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
                return tile_count > 0
        except Exception:
            return False

    def _append_log(self, log_path, line):
        try:
            with open(log_path, "a", encoding="utf-8") as log_file:
                log_file.write(f"{line}\n")
        except Exception:
            pass

    def _cleanup_partial_outputs(self, paths, completed_outputs):
        completed = {os.path.abspath(path) for path in completed_outputs}
        for path in paths:
            if not path:
                continue
            try:
                abs_path = os.path.abspath(path)
                if abs_path not in completed and os.path.exists(abs_path):
                    os.remove(abs_path)
            except Exception:
                pass

    def _build_jobs(self, features, field_name, group_by_field):
        if not group_by_field:
            jobs = []
            for feat in features:
                value = feat[field_name]
                raw_name = str(value).strip() if value not in [None, ""] else f"FID_{feat.id()}"
                jobs.append((raw_name, [feat]))
            return jobs

        grouped = defaultdict(list)
        for feat in features:
            value = feat[field_name]
            raw_name = str(value).strip() if value not in [None, ""] else "Tanpa_Nama"
            grouped[raw_name].append(feat)
        return [(name, feats) for name, feats in grouped.items()]

    def _make_mask_layer(self, source_layer, features, crs):
        is_multi = QgsWkbTypes.isMultiType(source_layer.wkbType())
        geom_text = "MultiPolygon" if is_multi else "Polygon"
        crs_authid = crs.authid() if crs and crs.isValid() else source_layer.crs().authid()
        uri = f"{geom_text}?crs={crs_authid}"
        mask = QgsVectorLayer(uri, "zonesculpt_mask", "memory")
        provider = mask.dataProvider()
        provider.addAttributes(source_layer.fields())
        mask.updateFields()
        if crs and crs.isValid():
            mask.setCrs(crs)

        transform = None
        source_crs = source_layer.crs()
        if self._is_usable_crs(source_crs) and crs and crs.isValid() and source_crs != crs:
            transform = QgsCoordinateTransform(source_crs, crs, QgsProject.instance())

        new_features = []
        for feat in features:
            new_feat = QgsFeature(mask.fields())
            geom = feat.geometry()
            if geom and not geom.isEmpty():
                geom = QgsGeometry(geom)
                if transform is not None:
                    try:
                        geom.transform(transform)
                    except Exception as exc:
                        raise RuntimeError(f"Failed to transform AOI geometry to Boundary CRS: {exc}")
                new_feat.setGeometry(geom)
            new_feat.setAttributes(feat.attributes())
            new_features.append(new_feat)

        provider.addFeatures(new_features)
        mask.updateExtents()
        return mask

    def _preflight_overlap_check(self, raster, aoi, features, source_crs, mask_crs):
        try:
            raster_crs = raster.crs() if raster and self._is_usable_crs(raster.crs()) else source_crs
            aoi_crs = aoi.crs() if aoi and self._is_usable_crs(aoi.crs()) else mask_crs
            raster_extent = raster.extent()

            aoi_extent = QgsRectangle()
            first = True
            for feat in features:
                geom = feat.geometry()
                if not geom or geom.isEmpty():
                    continue
                bbox = geom.boundingBox()
                if first:
                    aoi_extent = QgsRectangle(bbox)
                    first = False
                else:
                    aoi_extent.combineExtentWith(bbox)

            if first:
                return False, "The selected AOI features do not have valid geometry."

            aoi_extent_for_check = QgsRectangle(aoi_extent)
            if raster_crs and raster_crs.isValid() and aoi_crs and aoi_crs.isValid() and raster_crs != aoi_crs:
                transform = QgsCoordinateTransform(aoi_crs, raster_crs, QgsProject.instance())
                aoi_extent_for_check = transform.transformBoundingBox(aoi_extent_for_check)

            if not raster_extent.intersects(aoi_extent_for_check):
                return False, (
                    "AOI extent does not overlap the raster extent after CRS transformation.\n"
                    f"Raster CRS: {raster_crs.authid() or raster_crs.description()}\n"
                    f"AOI CRS: {aoi_crs.authid() or aoi_crs.description()}\n"
                    f"Raster extent: {raster_extent.toString()}\n"
                    f"AOI extent in raster CRS: {aoi_extent_for_check.toString()}"
                )
            return True, "OK"
        except Exception as exc:
            return False, f"Could not verify raster/AOI overlap: {exc}"

    def _safe_filename(self, text):
        text = unicodedata.normalize("NFKD", str(text))
        text = text.encode("ascii", "ignore").decode("ascii")
        text = re.sub(r"[\\/:*?\"<>|]+", "_", text)
        text = re.sub(r"\s+", "_", text)
        text = re.sub(r"_+", "_", text).strip("._ ")
        return text or "ZoneSculpt_Output"

    def _output_extension(self, output_format, raster):
        # Best-quality mode: never follow the input extension automatically.
        # This prevents MBTiles input from producing MBTiles output by default.
        if output_format.get("key") == "same":
            return ".tif"
        return output_format.get("ext") or ".tif"

    def _source_extension(self, source):
        clean_source = str(source).split("|")[0]
        _, ext = os.path.splitext(clean_source)
        ext = ext.lower()
        if ext == ".tiff":
            return ".tif"
        if ext == ".jpeg":
            return ".jpg"
        return ext

    def _unique_path(self, folder, base_name, used_names, extension):
        base = base_name
        counter = 1
        candidate = f"{base}{extension}"
        while candidate.lower() in used_names or os.path.exists(os.path.join(folder, candidate)):
            counter += 1
            candidate = f"{base}_{counter}{extension}"
        return os.path.join(folder, candidate)
