# -*- coding: utf-8 -*-
import os
from qgis.PyQt import uic
from qgis.PyQt.QtWidgets import QDialog, QComboBox, QTableWidget
from qgis.PyQt.QtCore import QVariant
from qgis.core import QgsProject, QgsWkbTypes, QgsVectorLayer, QgsMessageLog, Qgis

FORM_CLASS, _ = uic.loadUiType(os.path.join(
    os.path.dirname(__file__), 'chasm_calculator_dialog_base.ui'
))

# índices das colunas (mantidos p/ compatibilidade)
SHP1_COL  = 0
COLS1_COL = 1
GI1_COL   = 2
GO1_COL   = 3
SHP2_COL  = 4
COLS2_COL = 5
GI2_COL   = 6
GO2_COL   = 7

def _log(msg): QgsMessageLog.logMessage(str(msg), "Chasm", Qgis.Info)

class ChasmDialog(QDialog, FORM_CLASS):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setupUi(self)

        # widgets
        self.tbl = getattr(self, 'tblPairs', None)
        if self.tbl is None:
            # cria uma tabela oculta para manter compatibilidade com o código existente
            self.tbl = QTableWidget(self)
            self.tbl.setObjectName('tblPairs')
            self.tbl.setVisible(False)

        self.txtStatus        = getattr(self, 'txtStatus', None)
        self.btnAddRow        = getattr(self, 'btnAddRow', None)
        self.btnRemoveRows    = getattr(self, 'btnRemoveRows', None)
        self.btnRefreshLayers = getattr(self, 'btnRefreshLayers', None)
        # btnAddZips REMOVIDO

        # tabela
        self._ensure_pairs_table()

        # botões (apenas os que eventualmente existirem no .ui)
        if self.btnAddRow:        self.btnAddRow.clicked.connect(self.on_add_row)
        if self.btnRemoveRows:    self.btnRemoveRows.clicked.connect(self.on_remove_rows)
        if self.btnRefreshLayers: self.btnRefreshLayers.clicked.connect(self.refresh_all_layer_combos)

        # sDNA
        if hasattr(self, 'chkBetweenness'):
            self.chkBetweenness.toggled.connect(self._toggle_betweenness_children)

        # Combos de rede (linhas) e polígonos
        self._populate_network_layers()
        if hasattr(self, 'cbNetworkLayer'):
            self.cbNetworkLayer.currentIndexChanged.connect(self._populate_attribute_combos_network)

        if hasattr(self, 'cbPoligonoLayer'):
            self.cbPoligonoLayer.currentIndexChanged.connect(self._on_polygon_layer_combo_changed)
            self._on_polygon_layer_combo_changed()

        if hasattr(self, 'cbMetric'):    self.cbMetric.setCurrentText("ANGULAR")
        if hasattr(self, 'cbWeighting'): self.cbWeighting.setCurrentText("Link")

        # Preenche combos de atributos da rede
        self._populate_attribute_combos_network()

        self._append_status("Pronto. Use as combos de camadas já carregadas no projeto QGIS.")

    # ---------- util ----------
    def _append_status(self, msg: str):
        if self.txtStatus:
            self.txtStatus.appendPlainText(str(msg))

    # ---------- tabela ----------
    def _ensure_pairs_table(self):
        self.tbl.setAlternatingRowColors(True)
        self.tbl.setSelectionBehavior(self.tbl.SelectRows)
        self.tbl.setSelectionMode(self.tbl.ExtendedSelection)
        self.tbl.setColumnCount(8)
        self.tbl.setHorizontalHeaderLabels([
            "Shapefile 1 (polígonos)", "Colunas (shp1)", "GI (shp1)", "Outros (shp1)",
            "Shapefile 2 (rede)",     "Colunas (shp2)", "GI (shp2)", "Outros (shp2)"
        ])
        if self.tbl.horizontalHeader():
            self.tbl.horizontalHeader().setStretchLastSection(True)

    # cria (ou recupera) um QComboBox numa célula
    def _ensure_cell_combo(self, row: int, col: int, editable: bool) -> QComboBox:
        w = self.tbl.cellWidget(row, col)
        if not isinstance(w, QComboBox):
            w = QComboBox(self.tbl)
            w.setEditable(bool(editable))
            self.tbl.setCellWidget(row, col, w)
        return w

    def on_add_row(self):
        row = self.tbl.rowCount()
        self.tbl.insertRow(row)

        # combos de camada
        cmb_shp1 = self._ensure_cell_combo(row, SHP1_COL, False)
        cmb_shp2 = self._ensure_cell_combo(row, SHP2_COL, False)

        # combos de colunas
        self._ensure_cell_combo(row, COLS1_COL, False)
        self._ensure_cell_combo(row, COLS2_COL, False)

        # combos GI/GO
        self._ensure_cell_combo(row, GI1_COL, True)
        self._ensure_cell_combo(row, GO1_COL, True)
        self._ensure_cell_combo(row, GI2_COL, True)
        self._ensure_cell_combo(row, GO2_COL, True)

        # preencher listas de layers e focar índice 0
        any1 = self._fill_layer_combo(cmb_shp1, want='polygon')
        any2 = self._fill_layer_combo(cmb_shp2, want='line')
        if any1 and cmb_shp1.currentIndex() < 0: cmb_shp1.setCurrentIndex(0)
        if any2 and cmb_shp2.currentIndex() < 0: cmb_shp2.setCurrentIndex(0)

        # conectar
        cmb_shp1.currentIndexChanged.connect(lambda _ix, r=row: self._on_layer_changed(r, which=1))
        cmb_shp2.currentIndexChanged.connect(lambda _ix, r=row: self._on_layer_changed(r, which=2))

        # preencher já
        if cmb_shp1.count() > 0: self._on_layer_changed(row, which=1)
        if cmb_shp2.count() > 0: self._on_layer_changed(row, which=2)

    def on_remove_rows(self):
        sel = self.tbl.selectionModel().selectedRows() if self.tbl.selectionModel() else []
        rows = sorted({i.row() for i in sel}, reverse=True)
        for r in rows: self.tbl.removeRow(r)
        self._append_status(f"Removidas {len(rows)} linha(s).")

    # ---------- popular campos ----------
    def _on_layer_changed(self, row: int, which: int):
        cmb_layer = self._ensure_cell_combo(row, SHP1_COL if which == 1 else SHP2_COL, False)
        cmb_gi    = self._ensure_cell_combo(row, GI1_COL  if which == 1 else GI2_COL, True)
        cmb_go    = self._ensure_cell_combo(row, GO1_COL  if which == 1 else GO2_COL, True)
        cmb_cols  = self._ensure_cell_combo(row, COLS1_COL if which == 1 else COLS2_COL, False)

        lyr_id = cmb_layer.currentData()
        layer = QgsProject.instance().mapLayer(lyr_id) if lyr_id else None

        # limpa
        for c in (cmb_gi, cmb_go, cmb_cols):
            c.blockSignals(True)
            c.clear()

        if not (layer and layer.isValid()):
            cmb_cols.addItem("—")
            cmb_cols.setToolTip("")
            for c in (cmb_gi, cmb_go, cmb_cols): c.blockSignals(False)
            self._append_status(f"[linha {row+1}] layer inválido (id={lyr_id}).")
            return

        fields = [f.name() for f in layer.fields()]
        self._append_status(f"[linha {row+1} {'shp1' if which==1 else 'shp2'}] {layer.name()} -> {len(fields)} campo(s).")
        _log(f"on_layer_changed row={row} which={which} layer={layer.name()} fields={fields}")

        for n in fields:
            cmb_gi.addItem(n)
            cmb_go.addItem(n)
            cmb_cols.addItem(n)

        for combo, s in ((cmb_gi, "grupo_interesse"), (cmb_go, "grupo_outros")):
            ix = combo.findText(s)
            if ix >= 0: combo.setCurrentIndex(ix)

        cmb_cols.setToolTip("\n".join(fields))
        for c in (cmb_gi, cmb_go, cmb_cols): c.blockSignals(False)

    # ---------- combos de layers ----------
    def _fill_layer_combo(self, combo: QComboBox, want: str, preferred_id=None) -> bool:
        want = want.lower()
        combo.blockSignals(True)
        combo.clear()
        target_geom = {
            'polygon': QgsWkbTypes.PolygonGeometry,
            'line':    QgsWkbTypes.LineGeometry,
            'point':   QgsWkbTypes.PointGeometry
        }.get(want, None)

        idx_to_select = -1
        count_added = 0
        for lyr in QgsProject.instance().mapLayers().values():
            try:
                if target_geom is None or QgsWkbTypes.geometryType(lyr.wkbType()) == target_geom:
                    combo.addItem(lyr.name(), lyr.id())
                    count_added += 1
                    if preferred_id and lyr.id() == preferred_id:
                        idx_to_select = combo.count() - 1
            except Exception:
                continue

        if idx_to_select >= 0:
            combo.setCurrentIndex(idx_to_select)
        combo.blockSignals(False)
        return count_added > 0

    # ---------- sDNA ----------
    def _toggle_betweenness_children(self, checked: bool):
        if hasattr(self, 'chkBetweennessBidirectional'):
            self.chkBetweennessBidirectional.setEnabled(checked)

    def _populate_network_layers(self):
        # cbNetworkLayer (linhas)
        if hasattr(self, 'cbNetworkLayer'):
            cur = self.cbNetworkLayer.currentText() if self.cbNetworkLayer.count() else None
            self.cbNetworkLayer.clear()
            sel = -1
            for lyr in QgsProject.instance().mapLayers().values():
                try:
                    if QgsWkbTypes.geometryType(lyr.wkbType()) == QgsWkbTypes.LineGeometry:
                        self.cbNetworkLayer.addItem(lyr.name(), lyr.id())
                        if cur and lyr.name() == cur and sel < 0:
                            sel = self.cbNetworkLayer.count() - 1
                except Exception:
                    continue
            if sel >= 0:
                self.cbNetworkLayer.setCurrentIndex(sel)

        # cbPoligonoLayer (polígonos)
        if hasattr(self, 'cbPoligonoLayer'):
            cur = self.cbPoligonoLayer.currentText() if self.cbPoligonoLayer.count() else None
            self.cbPoligonoLayer.clear()
            sel = -1
            for lyr in QgsProject.instance().mapLayers().values():
                try:
                    if QgsWkbTypes.geometryType(lyr.wkbType()) == QgsWkbTypes.PolygonGeometry:
                        self.cbPoligonoLayer.addItem(lyr.name(), lyr.id())
                        if cur and lyr.name() == cur and sel < 0:
                            sel = self.cbPoligonoLayer.count() - 1
                except Exception:
                    continue
            if sel >= 0:
                self.cbPoligonoLayer.setCurrentIndex(sel)

    def _populate_attribute_combos_network(self):
        """
        Preenche Destination Weights (se existirem) com os campos da camada de rede.
        Defaults: g_in_exist, g_ou_exist, g_int_ns, g_ou_ns.
        """
        if not all(hasattr(self, n) for n in ("cbDW1", "cbDW2", "cbDW3", "cbDW4")):
            return

        lyr = self._current_layer_from_combo(self.cbNetworkLayer) if hasattr(self, "cbNetworkLayer") else None
        names = [f.name() for f in lyr.fields()] if (lyr and lyr.isValid()) else []

        def fill(combo: QComboBox, preferred: str = ""):
            if not combo: return
            combo.blockSignals(True)
            combo.clear()
            for n in names: combo.addItem(n)
            combo.setEditable(True)
            if preferred:
                ix = combo.findText(preferred)
                combo.setCurrentIndex(ix if ix >= 0 else -1)
                if ix < 0: combo.setEditText(preferred)
            combo.blockSignals(False)

        if hasattr(self, "cbOriginWeight"):
            fill(self.cbOriginWeight, "")

        fill(self.cbDW1, "g_in_exist")
        fill(self.cbDW2, "g_ou_exist")
        fill(self.cbDW3, "g_int_ns")
        fill(self.cbDW4, "g_ou_ns")

    # ===== helpers de campos =====
    def _fill_field_combo(self, combo: QComboBox, layer: QgsVectorLayer,
                          only_types=(QVariant.String, QVariant.Int, QVariant.LongLong, QVariant.Double),
                          placeholder="(sem campos compatíveis)"):
        if combo is None:
            return
        combo.blockSignals(True)
        combo.clear()
        if layer and layer.isValid():
            for f in layer.fields():
                try:
                    if f.type() in only_types:
                        combo.addItem(f.name(), f.name())
                except Exception:
                    continue
        if combo.count() == 0:
            combo.addItem(placeholder, None)
        combo.blockSignals(False)

    # --- suporte a ID do polígono e campos de grupo ---
    def _on_polygon_layer_combo_changed(self):
        if not hasattr(self, 'cbPoligonoLayer'):
            return

        lyr = self._current_layer_from_combo(self.cbPoligonoLayer)

        # 1) ID do polígono
        if hasattr(self, 'cbPoligonoIdField'):
            self.cbPoligonoIdField.blockSignals(True)
            self.cbPoligonoIdField.clear()
            if lyr and lyr.isValid():
                names = [f.name() for f in lyr.fields()]
                for n in names:
                    self.cbPoligonoIdField.addItem(n, n)
                pick = self._auto_pick_polygon_id_field(lyr)
                if pick:
                    ix = self.cbPoligonoIdField.findText(pick)
                    if ix >= 0:
                        self.cbPoligonoIdField.setCurrentIndex(ix)
            self.cbPoligonoIdField.blockSignals(False)

        # 2) Campos de grupo
        grp_int = getattr(self, 'cbGrupoInteresseField', None)
        grp_out = getattr(self, 'cbGrupoOutriField',    None)

        for combo in (grp_int, grp_out):
            if combo is not None:
                self._fill_field_combo(combo, lyr)

        def _try_pick(combo: QComboBox, *candidates):
            if combo is None or combo.count() == 0:
                return
            for cand in candidates:
                ix = combo.findText(cand)
                if ix >= 0:
                    combo.setCurrentIndex(ix)
                    return
            if combo.currentIndex() < 0 and combo.count() > 0:
                combo.setCurrentIndex(0)

        _try_pick(grp_int, "grupo_interesse", "gi", "GI")
        _try_pick(grp_out, "grupo_outros", "go", "GO")

    def _auto_pick_polygon_id_field(self, poly_layer):
        candidate_names = ['id', 'ID', 'gid', 'fid']
        names = [f.name() for f in poly_layer.fields()]
        for c in candidate_names:
            if c in names:
                return c
        return names[0] if names else None

    def _current_layer_from_combo(self, combo):
        lyr_id = combo.currentData() if combo and combo.count() else None
        return QgsProject.instance().mapLayer(lyr_id) if lyr_id else None

    # ---------- API externa (usada pelo chasm_calculator.py) ----------
    def refresh_all_layer_combos(self):
        for row in range(self.tbl.rowCount()):
            cmb_shp1 = self._ensure_cell_combo(row, SHP1_COL, False)
            cmb_shp2 = self._ensure_cell_combo(row, SHP2_COL, False)
            sel1 = cmb_shp1.currentData()
            sel2 = cmb_shp2.currentData()
            self._fill_layer_combo(cmb_shp1, 'polygon', sel1)
            self._fill_layer_combo(cmb_shp2, 'line', sel2)
            if cmb_shp1.count() > 0 and cmb_shp1.currentIndex() < 0: cmb_shp1.setCurrentIndex(0)
            if cmb_shp2.count() > 0 and cmb_shp2.currentIndex() < 0: cmb_shp2.setCurrentIndex(0)
            self._on_layer_changed(row, which=1)
            self._on_layer_changed(row, which=2)
        self._populate_network_layers()
        self._on_polygon_layer_combo_changed()
        self._populate_attribute_combos_network()
        self._append_status("Combos de layers/atributos atualizados.")

    # === Métodos esperados pelo chasm_calculator.py ===
    def selected_inputs(self):
        """
        Lê a tabela e devolve uma lista de dicts (mantido p/ compatibilidade):
        {
          'layer1_id': <polígono>, 'gi1': <texto>, 'go1': <texto>,
          'layer2_id': <linha>,    'gi2': <texto>, 'go2': <texto>,
          'cols1': [..], 'cols2': [..]
        }
        """
        results = []
        for row in range(self.tbl.rowCount()):
            cmb_shp1 = self._ensure_cell_combo(row, SHP1_COL, False)
            cmb_shp2 = self._ensure_cell_combo(row, SHP2_COL, False)
            cmb_gi1  = self._ensure_cell_combo(row, GI1_COL, True)
            cmb_go1  = self._ensure_cell_combo(row, GO1_COL, True)
            cmb_gi2  = self._ensure_cell_combo(row, GI2_COL, True)
            cmb_go2  = self._ensure_cell_combo(row, GO2_COL, True)
            cmb_c1   = self._ensure_cell_combo(row, COLS1_COL, False)
            cmb_c2   = self._ensure_cell_combo(row, COLS2_COL, False)

            layer1_id = cmb_shp1.currentData()
            layer2_id = cmb_shp2.currentData()
            gi1 = cmb_gi1.currentText().strip()
            go1 = cmb_go1.currentText().strip()
            gi2 = cmb_gi2.currentText().strip()
            go2 = cmb_go2.currentText().strip()

            col1 = cmb_c1.currentText().strip() if cmb_c1.count() else ""
            col2 = cmb_c2.currentText().strip() if cmb_c2.count() else ""

            if not layer1_id and not layer2_id:
                continue

            results.append({
                'layer1_id': layer1_id,
                'gi1': gi1, 'go1': go1, 'cols1': [col1] if col1 else [],
                'layer2_id': layer2_id,
                'gi2': gi2, 'go2': go2, 'cols2': [col2] if col2 else []
            })
        return results

    def selected_network_layer(self):
        if hasattr(self, 'cbNetworkLayer') and self.cbNetworkLayer.count():
            lyr_id = self.cbNetworkLayer.currentData()
            return QgsProject.instance().mapLayer(lyr_id) if lyr_id else None
        return None

    def sdna_params(self):
        bet = bool(getattr(self, 'chkBetweenness', None).isChecked()) if hasattr(self, 'chkBetweenness') else False
        betw_bi = bool(getattr(self, 'chkBetweennessBidirectional', None).isChecked()) if hasattr(self, 'chkBetweennessBidirectional') else False
        metric = self.cbMetric.currentText().strip() if hasattr(self, 'cbMetric') else "ANGULAR"
        weighting = self.cbWeighting.currentText().strip() if hasattr(self, 'cbWeighting') else ""

        dw_vals = self._dw_values_or_log()
        dw1, dw2, dw3, dw4 = (dw_vals + ["", "", "", ""])[:4]
        dest_weights = [dw1, dw2, dw3, dw4]

        origin_weight = self._read_text_like_combo(getattr(self, 'cbOriginWeight', None))

        radius = 1000
        for name in ('spnRadius', 'spinRadius', 'sbRadius', 'spinRadius'):
            if hasattr(self, name):
                try:
                    radius = int(getattr(self, name).value())
                    break
                except Exception:
                    pass

        radius_mode = "band"
        if hasattr(self, 'rbContinuous') and self.rbContinuous.isChecked():
            radius_mode = "radius"

        try:
            self._append_status(f"DW lidos: {dest_weights}")
        except Exception:
            pass

        return {
            "betweenness": bet,
            "betw_bidirectional": betw_bi,
            "metric": metric,
            "radius": radius,
            "radius_mode": radius_mode,
            "weighting": weighting,
            "origin_weight": origin_weight,
            "dest_weights": dest_weights
        }

    def _read_text_like_combo(self, w):
        if w is None:
            return ""
        try:
            if hasattr(w, "text"):
                return (w.text() or "").strip()
            if hasattr(w, "lineEdit") and callable(w.lineEdit):
                le = w.lineEdit()
                if le is not None:
                    return (le.text() or "").strip()
            if hasattr(w, "currentText"):
                return (w.currentText() or "").strip()
        except Exception:
            pass
        return ""

    def _dw_values_or_log(self):
        names = ("cbDW1","cbDW2","cbDW3","cbDW4")
        vals = []
        missing = []
        for n in names:
            combo = getattr(self, n, None)
            if combo is None:
                missing.append(n)
                vals.append("")
                continue
            try:
                le = combo.lineEdit()
                text = (le.text() if le else combo.currentText()) or ""
            except Exception:
                text = combo.currentText() or ""
            vals.append(text.strip())
        if missing:
            self._append_status(f"[ERRO] Combos inexistentes no UI: {', '.join(missing)}")
        else:
            self._append_status(f"DW lidos (UI OK): {vals}")
        return vals

    # ====== Suporte à fragmentação ======
    def selected_line_layer_id(self):
        if hasattr(self, 'cbNetworkLayer') and self.cbNetworkLayer.count():
            return self.cbNetworkLayer.currentData()
        for lyr in QgsProject.instance().mapLayers().values():
            try:
                if QgsWkbTypes.geometryType(lyr.wkbType()) == QgsWkbTypes.LineGeometry:
                    return lyr.id()
            except Exception:
                continue
        return None

    def selected_polygon_layer_id(self):
        if hasattr(self, 'cbPoligonoLayer') and self.cbPoligonoLayer.count():
            return self.cbPoligonoLayer.currentData()
        for lyr in QgsProject.instance().mapLayers().values():
            try:
                if QgsWkbTypes.geometryType(lyr.wkbType()) == QgsWkbTypes.PolygonGeometry:
                    return lyr.id()
            except Exception:
                continue
        return None

    def selected_polygon_id_field(self):
        poly_layer = None
        if hasattr(self, 'cbPoligonoLayer') and self.cbPoligonoLayer.count():
            poly_layer = self._current_layer_from_combo(self.cbPoligonoLayer)
        if hasattr(self, 'cbPoligonoIdField'):
            text = self.cbPoligonoIdField.currentText().strip()
            if text:
                return text
        if poly_layer and poly_layer.isValid():
            return self._auto_pick_polygon_id_field(poly_layer)
        return None
