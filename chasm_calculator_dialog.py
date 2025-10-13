# -*- coding: utf-8 -*-
import os, zipfile, tempfile, shutil
from qgis.PyQt import uic
from qgis.PyQt.QtWidgets import (
    QDialog, QFileDialog, QTableWidgetItem, QComboBox
)
from qgis.core import (
    QgsProject, QgsWkbTypes, QgsVectorLayer, QgsMessageLog, Qgis
)

FORM_CLASS, _ = uic.loadUiType(os.path.join(
    os.path.dirname(__file__), 'chasm_calculator_dialog_base.ui'
))

# colunas (modelo de "pares" no projeto)
SHP1_COL = 0; GI1_COL = 1; GO1_COL = 2
SHP2_COL = 3; GI2_COL = 4; GO2_COL = 5

class ChasmDialog(QDialog, FORM_CLASS):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setupUi(self)

        # ---------- localizar widgets por nome (compatível com UI antigo/novo) ----------
        # tabela
        self.tbl = getattr(self, 'tblPairs', None) or getattr(self, 'tblInputs', None)
        if self.tbl is None:
            raise AttributeError("UI não tem 'tblPairs' nem 'tblInputs' (tabela).")

        # status
        self.txtStatus = getattr(self, 'txtStatus', None)

        # botões (podem não existir no seu UI; se não existirem, ignora)
        self.btnAddRow        = getattr(self, 'btnAddRow', None)
        self.btnRemoveRows    = getattr(self, 'btnRemoveRows', None)
        self.btnRefreshLayers = getattr(self, 'btnRefreshLayers', None)
        self.btnAddZips       = getattr(self, 'btnAddZips', None)

        # configurar tabela para o modelo de pares (6 colunas)
        self._ensure_pairs_table()

        # conectar botões se existirem
        if self.btnAddRow:
            self.btnAddRow.clicked.connect(self.on_add_row)
        if self.btnRemoveRows:
            self.btnRemoveRows.clicked.connect(self.on_remove_rows)
        if self.btnRefreshLayers:
            self.btnRefreshLayers.clicked.connect(self.refresh_all_layer_combos)
        if self.btnAddZips:
            self.btnAddZips.clicked.connect(self.on_add_zips)  # opcional: carregar ZIPs no projeto

        # ---- sDNA ----
        if hasattr(self, 'chkBetweenness'):
            self.chkBetweenness.toggled.connect(self._toggle_betweenness_children)
        self._populate_network_layers()
        if hasattr(self, 'cbNetworkLayer'):
            self.cbNetworkLayer.currentIndexChanged.connect(self._populate_attribute_combos_network)
        if hasattr(self, 'cbMetric'):
            self.cbMetric.setCurrentText("ANGULAR")
        if hasattr(self, 'cbWeighting'):
            self.cbWeighting.setCurrentText("Link")

        self._append_status("Pronto. Adicione linhas e escolha camadas já carregadas no projeto.")

    # ===================== Setup da tabela =====================
    def _ensure_pairs_table(self):
        """Garante que a tabela tenha 6 colunas (pares) e cabeçalhos corretos."""
        try:
            self.tbl.setAlternatingRowColors(True)
            self.tbl.setSelectionBehavior(self.tbl.SelectRows)
            self.tbl.setSelectionMode(self.tbl.ExtendedSelection)
        except Exception:
            pass

        # Se a tabela era antiga (7 colunas com ZIP), reconfigura:
        try:
            self.tbl.setColumnCount(6)
            self.tbl.setHorizontalHeaderLabels([
                "Shapefile 1 (polígonos)", "GI (shp1)", "Outros (shp1)",
                "Shapefile 2 (rede)",     "GI (shp2)", "Outros (shp2)"
            ])
            self.tbl.horizontalHeader().setStretchLastSection(True)
        except Exception:
            pass

    # ===================== Importação: linhas/combos =====================
    def on_add_row(self):
        row = self.tbl.rowCount()
        self.tbl.insertRow(row)

        # Combos de layer (preenchidos a partir do projeto)
        cmb_shp1 = QComboBox(self.tbl)
        cmb_shp2 = QComboBox(self.tbl)
        self.tbl.setCellWidget(row, SHP1_COL, cmb_shp1)
        self.tbl.setCellWidget(row, SHP2_COL, cmb_shp2)

        # Combos de campos (editáveis)
        cmb_gi1 = QComboBox(self.tbl); cmb_gi1.setEditable(True)
        cmb_go1 = QComboBox(self.tbl); cmb_go1.setEditable(True)
        cmb_gi2 = QComboBox(self.tbl); cmb_gi2.setEditable(True)
        cmb_go2 = QComboBox(self.tbl); cmb_go2.setEditable(True)
        self.tbl.setCellWidget(row, GI1_COL, cmb_gi1)
        self.tbl.setCellWidget(row, GO1_COL, cmb_go1)
        self.tbl.setCellWidget(row, GI2_COL, cmb_gi2)
        self.tbl.setCellWidget(row, GO2_COL, cmb_go2)

        # Popular combos de layers conforme geometria
        self._fill_layer_combo(cmb_shp1, want='polygon')
        self._fill_layer_combo(cmb_shp2, want='line')

        # Conectar mudanças para preencher campos GI/GO
        cmb_shp1.currentIndexChanged.connect(lambda _ix, r=row: self._on_layer_changed(r, which=1))
        cmb_shp2.currentIndexChanged.connect(lambda _ix, r=row: self._on_layer_changed(r, which=2))

        # Disparar update de campos se houver seleção
        if cmb_shp1.count() > 0: self._on_layer_changed(row, which=1)
        if cmb_shp2.count() > 0: self._on_layer_changed(row, which=2)

    def on_remove_rows(self):
        sel = self.tbl.selectionModel().selectedRows() if self.tbl.selectionModel() else []
        rows = sorted({i.row() for i in sel}, reverse=True)
        for r in rows:
            self.tbl.removeRow(r)
        self._append_status(f"Removidas {len(rows)} linha(s).")

    def refresh_all_layer_combos(self):
        # re-popula combos de layers de todas as linhas (mantém seleção por id/nome se possível)
        for row in range(self.tbl.rowCount()):
            cmb_shp1 = self.tbl.cellWidget(row, SHP1_COL)
            cmb_shp2 = self.tbl.cellWidget(row, SHP2_COL)
            sel1 = cmb_shp1.currentData() if cmb_shp1 else None
            sel2 = cmb_shp2.currentData() if cmb_shp2 else None
            if cmb_shp1:
                self._fill_layer_combo(cmb_shp1, want='polygon', preferred_id=sel1)
                self._on_layer_changed(row, which=1)
            if cmb_shp2:
                self._fill_layer_combo(cmb_shp2, want='line', preferred_id=sel2)
                self._on_layer_changed(row, which=2)
        self._populate_network_layers()
        self._append_status("Listas de camadas atualizadas a partir do projeto.")

    def _on_layer_changed(self, row: int, which: int):
        cmb_layer = self.tbl.cellWidget(row, SHP1_COL if which == 1 else SHP2_COL)
        cmb_gi    = self.tbl.cellWidget(row, GI1_COL  if which == 1 else GI2_COL)
        cmb_go    = self.tbl.cellWidget(row, GO1_COL  if which == 1 else GO2_COL)
        if not (cmb_layer and cmb_gi and cmb_go): return

        lyr_id = cmb_layer.currentData()
        layer = QgsProject.instance().mapLayer(lyr_id) if lyr_id else None
        cmb_gi.clear(); cmb_go.clear()
        if not layer:
            return

        fields = [f.name() for f in layer.fields()]
        for n in fields:
            cmb_gi.addItem(n)
            cmb_go.addItem(n)

        # sugestões
        for combo, s in ((cmb_gi, "grupo_interesse"), (cmb_go, "grupo_outros")):
            ix = combo.findText(s)
            if ix >= 0: combo.setCurrentIndex(ix)

        self._append_status(f"[linha {row+1} {'shp1' if which==1 else 'shp2'}] {layer.name()} — {len(fields)} campo(s).")

    # ===================== Helpers: camadas do projeto =====================
    def _fill_layer_combo(self, combo: QComboBox, want: str, preferred_id=None):
        """want: 'polygon' | 'line' | 'point'."""
        want = want.lower()
        keep_text = combo.currentText()
        combo.clear()
        target_geom = {
            'polygon': QgsWkbTypes.PolygonGeometry,
            'line':    QgsWkbTypes.LineGeometry,
            'point':   QgsWkbTypes.PointGeometry
        }.get(want, None)

        idx_to_select = -1
        for lyr in QgsProject.instance().mapLayers().values():
            try:
                if target_geom is None or QgsWkbTypes.geometryType(lyr.wkbType()) == target_geom:
                    combo.addItem(lyr.name(), lyr.id())
                    if preferred_id and lyr.id() == preferred_id:
                        idx_to_select = combo.count()-1
            except Exception:
                continue

        if idx_to_select >= 0:
            combo.setCurrentIndex(idx_to_select)
        elif keep_text:
            ix = combo.findText(keep_text)
            if ix >= 0: combo.setCurrentIndex(ix)

    # ===================== ZIP -> carregar no projeto (opcional) =====================
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
        self._append_status(f"{added} camada(s) carregada(s) a partir dos ZIP(s).")
        self.refresh_all_layer_combos()

    def _load_layer_from_zip(self, zip_path: str, shp_inside: str):
        """/vsizip/ + fallback extração temporária."""
        norm_zip = os.path.normpath(zip_path).replace('\\', '/')
        vsi_path = f"/vsizip/{norm_zip}/{shp_inside}"
        layer_name = os.path.splitext(os.path.basename(shp_inside))[0]

        v = QgsVectorLayer(vsi_path, layer_name, "ogr")
        if v and v.isValid():
            for lyr in QgsProject.instance().mapLayers().values():
                try:
                    if lyr.source() == v.source() and lyr.name() == v.name():
                        return lyr
                except Exception:
                    pass
            QgsProject.instance().addMapLayer(v)
            return v

        # fallback: extrai sidecars para tmp
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
                        if ext == '.shp':
                            main = out
            if not main: return None
            v2 = QgsVectorLayer(main, layer_name, "ogr")
            if v2 and v2.isValid():
                QgsProject.instance().addMapLayer(v2)
                return v2
        except Exception as e:
            QgsMessageLog.logMessage(f"ZIP fallback erro: {e}", "Chasm", Qgis.Critical)
        return None

    def _append_status(self, msg: str):
        if self.txtStatus:
            self.txtStatus.appendPlainText(msg)

    # ===================== sDNA helpers =====================
    def _toggle_betweenness_children(self, checked: bool):
        if hasattr(self, 'chkBetweennessBidirectional'):
            self.chkBetweennessBidirectional.setEnabled(checked)

    def _populate_network_layers(self):
        if not hasattr(self, 'cbNetworkLayer'): return
        cur_name = self.cbNetworkLayer.currentText() if self.cbNetworkLayer.count() else None
        self.cbNetworkLayer.clear()
        found_index = -1
        for lyr in QgsProject.instance().mapLayers().values():
            try:
                if QgsWkbTypes.geometryType(lyr.wkbType()) == QgsWkbTypes.LineGeometry:
                    self.cbNetworkLayer.addItem(lyr.name(), lyr.id())
                    if lyr.name() == cur_name and found_index == -1:
                        found_index = self.cbNetworkLayer.count() - 1
            except Exception:
                continue
        if found_index >= 0:
            self.cbNetworkLayer.setCurrentIndex(found_index)

    def _populate_attribute_combos_network(self):
        pass

    # ===================== Getters p/ o plugin =====================
    def selected_inputs(self):
        """
        [{'layer1_id': '...', 'gi1':'campo', 'go1':'campo',
          'layer2_id': '...', 'gi2':'campo', 'go2':'campo'}]
        """
        data = []
        for row in range(self.tbl.rowCount()):
            cmb_shp1 = self.tbl.cellWidget(row, SHP1_COL)
            cmb_gi1  = self.tbl.cellWidget(row, GI1_COL)
            cmb_go1  = self.tbl.cellWidget(row, GO1_COL)
            cmb_shp2 = self.tbl.cellWidget(row, SHP2_COL)
            cmb_gi2  = self.tbl.cellWidget(row, GI2_COL)
            cmb_go2  = self.tbl.cellWidget(row, GO2_COL)
            if not (cmb_shp1 and cmb_gi1 and cmb_go1 and cmb_shp2 and cmb_gi2 and cmb_go2):
                continue
            layer1_id = cmb_shp1.currentData()
            layer2_id = cmb_shp2.currentData()
            gi1 = (cmb_gi1.currentText() or "").strip()
            go1 = (cmb_go1.currentText() or "").strip()
            gi2 = (cmb_gi2.currentText() or "").strip()
            go2 = (cmb_go2.currentText() or "").strip()
            if not (layer1_id or layer2_id):
                continue
            data.append({
                'layer1_id': layer1_id, 'gi1': gi1, 'go1': go1,
                'layer2_id': layer2_id, 'gi2': gi2, 'go2': go2
            })
        self._append_status(f"selected_inputs -> {len(data)} linha(s).")
        return data

    def selected_network_layer(self):
        if not hasattr(self, 'cbNetworkLayer'): return None
        idx = self.cbNetworkLayer.currentIndex()
        if idx < 0: return None
        lyr_id = self.cbNetworkLayer.currentData()
        return QgsProject.instance().mapLayer(lyr_id)

    def sdna_params(self) -> dict:
        # Se algum widget não existir no seu UI, retorna valores padrão razoáveis
        def _val(name, default=None):
            w = getattr(self, name, None)
            if w is None: return default
            # tipos comuns
            if name in ('chkBetweenness','chkBetweennessBidirectional'):
                return bool(w.isChecked())
            if name == 'cbMetric' or name == 'cbWeighting':
                return w.currentText()
            if name == 'spinRadius':
                return int(w.value())
            if name == 'rbBand':
                return bool(w.isChecked())
            if name.startswith('cbDW'):
                return (w.currentText() or '').strip()
            if name == 'cbOriginWeight':
                return (w.currentText() or '').strip()
            return default

        return {
            "betweenness": _val('chkBetweenness', False),
            "betw_bidirectional": _val('chkBetweennessBidirectional', False),
            "metric": _val('cbMetric', 'ANGULAR'),
            "radius": _val('spinRadius', 1600),
            "radius_mode": "band" if _val('rbBand', True) else "continuous",
            "weighting": _val('cbWeighting', 'Link'),
            "origin_weight": _val('cbOriginWeight', None) or None,
            "dest_weights": [
                _val('cbDW1',''),
                _val('cbDW2',''),
                _val('cbDW3',''),
                _val('cbDW4',''),
            ]
        }
