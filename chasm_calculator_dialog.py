# -*- coding: utf-8 -*-
import os, zipfile
from qgis.PyQt import uic
from qgis.PyQt.QtWidgets import QDialog, QFileDialog
from qgis.core import QgsProject, QgsWkbTypes, QgsVectorLayer

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
        self.cbShp1.currentIndexChanged.connect(self.on_shp1_changed)
        self._append_status("Pronto. Selecione um .zip com shapefile(s).")

        # sDNA
        self.chkBetweenness.toggled.connect(self._toggle_betweenness_children)

        # popular camadas de linha já carregadas (opcional)
        self._populate_network_layers()
        self.cbNetworkLayer.currentIndexChanged.connect(self._populate_attribute_combos_network)

        # defaults
        self.cbMetric.setCurrentText("ANGULAR")
        self.cbWeighting.setCurrentText("Link")

    # ---------------- ZIP ----------------
    def on_browse_zip(self):
        path, _ = QFileDialog.getOpenFileName(self, "Selecionar arquivo .zip", "", "Arquivos ZIP (*.zip)")
        if path:
            self.leZipPath.setText(path)

    def on_zip_changed(self, path: str):
        # limpa combos de shp e campos
        self.cbShp1.clear()
        self.cbShp2.clear()
        self.cbColGrupoInteresse.clear()
        self.cbColGrupoOutros.clear()

        if not path or not os.path.isfile(path):
            self._append_status("Informe um .zip válido.")
            return
        try:
            shp_list = self._list_shp_in_zip(path)
            if not shp_list:
                self._append_status("Nenhum .shp encontrado dentro do .zip.")
                return
            self.cbShp1.addItems(shp_list)
            self.cbShp2.addItems(shp_list)
            self._append_status(f"Encontrados {len(shp_list)} shapefile(s) no .zip.")
        except Exception as e:
            self._append_status(f"Erro lendo .zip: {e}")

    def on_shp1_changed(self, idx: int):
        # Ao trocar o Shapefile 1, ler campos e preencher os combos de colunas
        self.cbColGrupoInteresse.clear()
        self.cbColGrupoOutros.clear()
        shp_inside = self.selected_shp1()
        zip_path = self.selected_zip()
        if not shp_inside or not zip_path:
            return

        # monta /vsizip/ e abre layer temporária só pra listar campos
        norm_zip = os.path.normpath(zip_path).replace('\\', '/')
        vsi_path = f"/vsizip/{norm_zip}/{shp_inside}"
        layer = QgsVectorLayer(vsi_path, "__tmp__", "ogr")
        if not layer or not layer.isValid():
            self._append_status("Não foi possível ler campos do Shapefile 1.")
            return

        fields = [f.name() for f in layer.fields()]
        for name in fields:
            self.cbColGrupoInteresse.addItem(name)
            self.cbColGrupoOutros.addItem(name)

        # sugestões de nomes esperados
        for (combo, suggestion) in [
            (self.cbColGrupoInteresse, "grupo_interesse"),
            (self.cbColGrupoOutros, "grupo_outros")
        ]:
            ix = combo.findText(suggestion)
            if ix >= 0:
                combo.setCurrentIndex(ix)

        self._append_status(f"Campos carregados do Shapefile 1 ({len(fields)} campo(s)).")

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

    def _populate_attribute_combos_network(self):
        # futuro: se quiser preencher combos com campos da camada de rede
        pass

    # --------------- getters -------------
    def selected_zip(self) -> str:
        return self.leZipPath.text().strip()

    def selected_shp1(self) -> str:
        return self.cbShp1.currentText().strip() if self.cbShp1.count() else ""

    def selected_shp2(self) -> str:
        return self.cbShp2.currentText().strip() if self.cbShp2.count() else ""

    def selected_columns_from_shp1(self):
        return (
            self.cbColGrupoInteresse.currentText().strip(),
            self.cbColGrupoOutros.currentText().strip()
        )

    def selected_network_layer(self):
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
