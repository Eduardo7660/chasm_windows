# -*- coding: utf-8 -*-
import os

from qgis.PyQt.QtCore import QSettings, QTranslator, QCoreApplication
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QMessageBox
from qgis.core import (
    QgsVectorLayer, QgsProject, QgsMessageLog, Qgis, QgsWkbTypes, QgsApplication
)

# resources é leve; se faltar, não derruba a classe
try:
    from .resources import *
except Exception:
    pass


class Chasm:
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        # locale
        try:
            locale = (QSettings().value('locale/userLocale') or '')[0:2]
        except Exception:
            locale = ''
        locale_path = os.path.join(self.plugin_dir, 'i18n', f'Chasm_{locale}.qm')
        if os.path.exists(locale_path):
            self.translator = QTranslator()
            self.translator.load(locale_path)
            QCoreApplication.installTranslator(self.translator)

        self.actions = []
        self.menu = self.tr(u'&Chasm Calculator')
        self.first_start = True
        self.dlg = None  # será importado e criado no run()

    def tr(self, message):
        return QCoreApplication.translate('Chasm', message)

    def add_action(self, icon_path, text, callback, enabled_flag=True,
                   add_to_menu=True, add_to_toolbar=True, status_tip=None,
                   whats_this=None, parent=None):
        action = QAction(QIcon(icon_path), text, parent)
        action.triggered.connect(callback)
        action.setEnabled(enabled_flag)
        if status_tip:
            action.setStatusTip(status_tip)
        if whats_this:
            action.setWhatsThis(whats_this)
        if add_to_toolbar:
            self.iface.addToolBarIcon(action)
        if add_to_menu:
            self.iface.addPluginToMenu(self.menu, action)
        self.actions.append(action)
        return action

    def initGui(self):
        icon_path = ':/plugins/chasm_calculator/icon.png'
        self.add_action(icon_path, text=self.tr(u'Chasm Calculator'),
                        callback=self.run, parent=self.iface.mainWindow())

    def unload(self):
        for action in self.actions:
            self.iface.removePluginMenu(self.tr(u'&Chasm Calculator'), action)
            self.iface.removeToolBarIcon(action)

    # ------------------------------ RUN ------------------------------
    def run(self):
        """Adia o import do diálogo para evitar falhar na importação do módulo."""
        if self.dlg is None:
            try:
                from .chasm_calculator_dialog import ChasmDialog  # <-- import adiado
            except Exception as e:
                msg = f"Falha ao importar chasm_calculator_dialog:\n{e}"
                QgsMessageLog.logMessage(msg, "chasm_calculator", Qgis.Critical)
                QMessageBox.critical(self.iface.mainWindow(), "Chasm", msg)
                return
            try:
                self.dlg = ChasmDialog(self.iface.mainWindow())
            except Exception as e:
                msg = f"Falha ao criar o diálogo ChasmDialog:\n{e}"
                QgsMessageLog.logMessage(msg, "chasm_calculator", Qgis.Critical)
                QMessageBox.critical(self.iface.mainWindow(), "Chasm", msg)
                return

        self.dlg.show()
        if not self.dlg.exec_():
            return

        # ===== A partir daqui é o fluxo normal =====
        # Coleta das entradas (agora vêm como IDs de camadas, do seu diálogo novo)
        try:
            inputs = self.dlg.selected_inputs()  # [{'layer1_id':..., 'gi1':..., ...}, ...]
        except Exception as e:
            QMessageBox.critical(self.iface.mainWindow(), "Chasm", f"Erro lendo entradas:\n{e}")
            return

        if not inputs:
            QMessageBox.warning(self.iface.mainWindow(), "Chasm",
                                "Adicione ao menos uma linha (camadas do projeto).")
            return

        loaded_layers = []

        # No modelo atual você escolhe camadas já carregadas; apenas referencia:
        for ent in inputs:
            for key in ('layer1_id', 'layer2_id'):
                lyr_id = ent.get(key)
                if not lyr_id:
                    continue
                lyr = QgsProject.instance().mapLayer(lyr_id)
                if lyr and lyr not in loaded_layers:
                    loaded_layers.append(lyr)

        if not loaded_layers:
            QMessageBox.critical(self.iface.mainWindow(), "Chasm",
                                 "Não foi possível referenciar camadas válidas do projeto.")
            return

        # Camada de rede (da aba sDNA ou autodetecta LINHAS)
        net_layer = self.dlg.selected_network_layer()
        if net_layer is None:
            for lyr in loaded_layers:
                try:
                    if QgsWkbTypes.geometryType(lyr.wkbType()) == QgsWkbTypes.LineGeometry:
                        net_layer = lyr
                        break
                except Exception:
                    pass
        if net_layer is None:
            QMessageBox.warning(self.iface.mainWindow(), "Chasm",
                                "Selecione a camada de sistema viário (linhas) na aba sDNA.")
            return

        # Parâmetros sDNA
        params = self.dlg.sdna_params()
        dest_weights = [w for w in params.get("dest_weights", []) if w]
        if len(dest_weights) != 4:
            QMessageBox.warning(self.iface.mainWindow(), "Chasm",
                                "Preencha os quatro 'Destination weight'.")
            return

        # (Opcional) Execução sDNA — deixe ativo se já estiver com provider OK
        try:
            import processing

            def _find_sdna_integral_alg():
                reg = QgsApplication.processingRegistry()
                for cand in ('sdna:integral', 'sdna:integralanalysis', 'sdna:integral_analysis'):
                    if reg.algorithmById(cand) is not None:
                        return cand
                for prov in reg.providers():
                    if 'sdna' in prov.id().lower():
                        for alg in prov.algorithms():
                            if 'integral' in alg.id().lower() or 'integral' in alg.displayName().lower():
                                return alg.id()
                return None

            alg_id = _find_sdna_integral_alg()
            if not alg_id:
                raise RuntimeError("Algoritmo sDNA Integral não encontrado no Processing.")

            par_base = {
                'input_polyline_features': net_layer,
                'compute_betweenness': params["betweenness"],
                'compute_betweenness_bidirectional': params["betw_bidirectional"],
                'analysis_metric': params["metric"],
                'radius': params["radius"],
                'radius_mode': 0 if params["radius_mode"] == 'band' else 1,
                'weighting': params["weighting"],
                'origin_weight': params["origin_weight"] or '',
                'output_features': 'TEMPORARY_OUTPUT'
            }

            results_added = 0
            for i, dw in enumerate(dest_weights, start=1):
                par = dict(par_base); par['destination_weight'] = dw
                res = processing.run(alg_id, par)
                out = res.get('output_features')
                if out:
                    out.setName(f"{net_layer.name()}_MAD_run{i}")
                    QgsProject.instance().addMapLayer(out)
                    results_added += 1

            QMessageBox.information(self.iface.mainWindow(), "Chasm",
                                    f"sDNA Integral executado ({results_added} saídas).")
        except Exception as e:
            QgsMessageLog.logMessage(f"sDNA erro: {e}", "chasm_calculator", Qgis.Critical)
            QMessageBox.critical(self.iface.mainWindow(), "Chasm", f"Falha ao executar sDNA:\n{e}")
