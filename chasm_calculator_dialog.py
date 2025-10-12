# -*- coding: utf-8 -*-
import os, zipfile
from qgis.PyQt import uic
from qgis.PyQt.QtWidgets import QDialog, QFileDialog
from qgis.core import QgsProject, QgsWkbTypes

FORM_CLASS, _ = uic.loadUiType(os.path.join(
    os.path.dirname(__file__), 'chasm_calculator_dialog_base.ui'
))

class ChasmDialog(QDialog, FORM_CLASS):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setupUi(self)

        # ZIP
        self.btnBrowse.clicked.connect(self.on_browse_zip)
        self.leZipPath.textChanged.connect(self.on_zip_changed)
        self.cbShp.currentIndexChanged.connect(self.on_shp_changed)
        self._append_status("Pronto. Selecione um .zip com shapefile(s).")

        # sDNA
        self.chkBetweenness.toggled.connect(self._toggle_betweenness_children)

        self._populate_network_layers()
        self.cbNetworkLayer.currentIndexChanged.connect(self._populate_attribute_combos)

        # defaults recomendados
        self.cbMetric.setCurrentText("ANGULAR")
        self.cbWeighting.setCurrentText("Link")

        # primeira carga de atributos (se houver camada)
        self._populate_attribute_combos()

    # ---------------- ZIP ----------------
    def on_browse_zip(self):
        path, _ = QFileDialog.getOpenFileName(self, "Selecionar arquivo .zip", "", "Arquivos ZIP (*.zip)")
        if path:
            self.leZipPath.setText(path)

    def on_zip_changed(self, path: str):
        self.cbShp.clear()
        if not path or not os.path.isfile(path):
            self._append_status("Informe um .zip válido.")
            return
        try:
            shp_list = self._list_shp_in_zip(path)
            if not shp_list:
                self._append_status("Nenhum .shp encontrado dentro do .zip.")
                return
            self.cbShp.addItems(shp_list)
            self._append_status(f"Encontrados {len(shp_list)} shapefile(s) no .zip.")
        except Exception as e:
            self._append_status(f"Erro lendo .zip: {e}")

    def on_shp_changed(self, idx: int):
        if idx >= 0 and self.cbShp.count() > 0:
            self._append_status(f"Selecionado: {self.cbShp.currentText()}")

    def _list_shp_in_zip(self, zip_path: str):
        shp_paths = []
        with zipfile.ZipFile(zip_path, 'r') as z:
            for name in z.namelist():
                if name.lower().endswith('.shp') and not name.endswith('/'):
                    shp_paths.append(name)
        return sorted(shp_paths, key=lambda s: s.lower())

    def _append_status(self, msg: str):
        self.txtStatus.appendPlainText(msg)

    # --------------- sDNA ----------------
    def _toggle_betweenness_children(self, checked: bool):
        self.chkBetweennessBidirectional.setEnabled(checked)

    def _populate_network_layers(self):
        self.cbNetworkLayer.clear()
        for lyr in QgsProject.instance().mapLayers().values():
            try:
                if QgsWkbTypes.geometryType(lyr.wkbType()) == QgsWkbTypes.LineGeometry:
                    self.cbNetworkLayer.addItem(lyr.name(), lyr.id())
            except Exception:
                continue

    def _populate_attribute_combos(self):
        # limpa combos de campos
        for combo in (self.cbOriginWeight, self.cbDW1, self.cbDW2, self.cbDW3, self.cbDW4):
            combo.clear()
            combo.setEditable(True)

        lyr = self.selected_network_layer()
        if not lyr:
            return

        fields = lyr.fields()
        for f in fields:
            # adiciona todos; se quiser só numéricos, cheque f.typeName()
            for combo in (self.cbOriginWeight, self.cbDW1, self.cbDW2, self.cbDW3, self.cbDW4):
                combo.addItem(f.name())

        # sugestões de nomes típicos
        for name, combo in (("g_in_exist", self.cbDW1),
                            ("g_ou_exist", self.cbDW2),
                            ("g_int_ns",  self.cbDW3),
                            ("g_ou_ns",   self.cbDW4)):
            idx = combo.findText(name)
            if idx >= 0:
                combo.setCurrentIndex(idx)

    # --------------- getters -------------
    def selected_zip(self) -> str:
        return self.leZipPath.text().strip()

    def selected_shp_inside_zip(self) -> str:
        return self.cbShp.currentText().strip() if self.cbShp.count() else ""

    def selected_network_layer(self):
        from qgis.core import QgsProject
        idx = self.cbNetworkLayer.currentIndex()
        if idx < 0: return None
        lyr_id = self.cbNetworkLayer.currentData()
        return QgsProject.instance().mapLayer(lyr_id)

    def sdna_params(self) -> dict:
        """Retorna os parâmetros selecionados (para chamar o sDNA futuramente)."""
        return {
            "betweenness": self.chkBetweenness.isChecked(),
            "betw_bidirectional": self.chkBetweennessBidirectional.isChecked(),
            "metric": self.cbMetric.currentText(),
            "radius": self.spinRadius.value(),
            "radius_mode": "band" if self.rbBand.isChecked() else "continuous",
            "weighting": self.cbWeighting.currentText(),
            "origin_weight": self.cbOriginWeight.currentText().strip() or None,
            "dest_weights": [
                self.cbDW1.currentText().strip(),
                self.cbDW2.currentText().strip(),
                self.cbDW3.currentText().strip(),
                self.cbDW4.currentText().strip(),
            ]
        }
