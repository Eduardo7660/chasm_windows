# -*- coding: utf-8 -*-
import os, zipfile, tempfile, shutil
from qgis.PyQt import uic
from qgis.PyQt.QtWidgets import QDialog, QFileDialog, QComboBox
from qgis.core import QgsProject, QgsWkbTypes, QgsVectorLayer, QgsMessageLog, Qgis

FORM_CLASS, _ = uic.loadUiType(os.path.join(
    os.path.dirname(__file__), 'chasm_calculator_dialog_base.ui'
))

# índices das colunas
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
            raise AttributeError("UI precisa ter QTableWidget com objectName='tblPairs'")
        self.txtStatus       = getattr(self, 'txtStatus', None)
        self.btnAddRow       = getattr(self, 'btnAddRow', None)
        self.btnRemoveRows   = getattr(self, 'btnRemoveRows', None)
        self.btnRefreshLayers= getattr(self, 'btnRefreshLayers', None)
        self.btnAddZips      = getattr(self, 'btnAddZips', None)

        # tabela
        self._ensure_pairs_table()

        # botões
        if self.btnAddRow:        self.btnAddRow.clicked.connect(self.on_add_row)
        if self.btnRemoveRows:    self.btnRemoveRows.clicked.connect(self.on_remove_rows)
        if self.btnRefreshLayers: self.btnRefreshLayers.clicked.connect(self.refresh_all_layer_combos)
        if self.btnAddZips:       self.btnAddZips.clicked.connect(self.on_add_zips)

        # sDNA
        if hasattr(self, 'chkBetweenness'):
            self.chkBetweenness.toggled.connect(self._toggle_betweenness_children)
        self._populate_network_layers()
        if hasattr(self, 'cbNetworkLayer'):
            self.cbNetworkLayer.currentIndexChanged.connect(self._populate_attribute_combos_network)
        if hasattr(self, 'cbMetric'):    self.cbMetric.setCurrentText("ANGULAR")
        if hasattr(self, 'cbWeighting'): self.cbWeighting.setCurrentText("Link")

        self._append_status("Dialog pronto. Use 'Adicionar linha' e escolha as camadas.")

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
        # garanta que TODOS os combos existem
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

    # ---------- ZIP ----------
    def on_add_zips(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Selecionar ZIP(s)", "", "Arquivos ZIP (*.zip)")
        if not paths: return
        added = 0
        for p in paths:
            if not os.path.isfile(p): continue
            with zipfile.ZipFile(p, 'r') as z:
                for name in z.namelist():
                    if name.lower().endswith('.shp') and not name.endswith('/'):
                        lyr = self._load_layer_from_zip(p, name)
                        if lyr: added += 1
        self._append_status(f"{added} camada(s) carregada(s) de ZIP(s).")
        self.refresh_all_layer_combos()

    def _load_layer_from_zip(self, zip_path: str, shp_inside: str):
        norm_zip = os.path.normpath(zip_path).replace('\\', '/')
        vsi_path = f"/vsizip/{norm_zip}/{shp_inside}"
        layer_name = os.path.splitext(os.path.basename(shp_inside))[0]
        v = QgsVectorLayer(vsi_path, layer_name, "ogr")
        if v and v.isValid() and len(v.fields()) > 0:
            QgsProject.instance().addMapLayer(v)
            return v

        base = os.path.splitext(shp_inside)[0]
        exts = ('.shp', '.dbf', '.shx', '.prj', '.cpg', '.qpj', '.shp.xml')
        tmp = tempfile.mkdtemp(prefix="chasm_zip_")
        main = None
        try:
            with zipfile.ZipFile(zip_path, 'r') as z:
                members = set(z.namelist())
                for ext in exts:
                    name = base + ext
                    if name in members:
                        out = os.path.join(tmp, os.path.basename(name))
                        with z.open(name) as src, open(out, 'wb') as dst:
                            shutil.copyfileobj(src, dst)
                        if ext == '.shp': main = out
            if not main: return None
            v2 = QgsVectorLayer(main, layer_name, "ogr")
            if v2 and v2.isValid() and len(v2.fields()) > 0:
                QgsProject.instance().addMapLayer(v2)
                return v2
        except Exception as e:
            QgsMessageLog.logMessage(f"ZIP fallback erro: {e}", "Chasm", Qgis.Critical)
        return None

    # ---------- sDNA ----------
    def _toggle_betweenness_children(self, checked: bool):
        if hasattr(self, 'chkBetweennessBidirectional'):
            self.chkBetweennessBidirectional.setEnabled(checked)

    def _populate_network_layers(self):
        if not hasattr(self, 'cbNetworkLayer'): return
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

        if not hasattr(self, 'cbPoligonoLayer'): return
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
        pass

    # ---------- API externa ----------
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
        self._append_status("Combos de layers/atributos atualizados.")
