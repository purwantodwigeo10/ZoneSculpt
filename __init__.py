# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
"""QGIS entry point for ZoneSculpt."""


def classFactory(iface):
    from .zone_sculpt import ZoneSculptPlugin
    return ZoneSculptPlugin(iface)
