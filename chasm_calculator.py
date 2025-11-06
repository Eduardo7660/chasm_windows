# -*- coding: utf-8 -*-
import os

from qgis.PyQt.QtCore import QSettings, QTranslator, QCoreApplication, QVariant
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QMessageBox
from qgis.core import (
    QgsVectorLayer, QgsProject, QgsMessageLog, Qgis, QgsWkbTypes, QgsApplication,
    QgsField
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
        self._sdna_integral_alg_id = None

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

        # Botão de teste — fragmentar linhas por polígonos usando camadas selecionadas
        self.add_action(icon_path, text=self.tr(u'Fragmentar Linhas por Polígonos (teste)'),
                        callback=self.do_fragmentation_test, parent=self.iface.mainWindow())

        # tenta cachear o alg sDNA (não obrigatório pro teste)
        try:
            import processing  # garante provider registrado
            self._sdna_integral_alg_id = self._find_sdna_integral_alg()
        except Exception:
            self._sdna_integral_alg_id = None

    def unload(self):
        for action in self.actions:
            self.iface.removePluginMenu(self.tr(u'&Chasm Calculator'), action)
            self.iface.removeToolBarIcon(action)
        if hasattr(self, "translator"):
            try:
                QCoreApplication.removeTranslator(self.translator)
            except Exception:
                pass

    # ------------------------------ Helpers ------------------------------
    def _msg(self, text, level=Qgis.Info, duration=6):
        try:
            self.iface.messageBar().pushMessage("Chasm", text, level=level, duration=duration)
        except Exception:
            if level == Qgis.Critical:
                QMessageBox.critical(self.iface.mainWindow(), "Chasm", text)
            elif level == Qgis.Warning:
                QMessageBox.warning(self.iface.mainWindow(), "Chasm", text)
            else:
                QMessageBox.information(self.iface.mainWindow(), "Chasm", text)

    def _find_sdna_integral_alg(self):
        try:
            reg = QgsApplication.processingRegistry()
            for cand in ('sdna:integral', 'sdna:integralanalysis', 'sdna:integral_analysis'):
                if reg.algorithmById(cand) is not None:
                    return cand
            for prov in reg.providers():
                if 'sdna' in prov.id().lower():
                    for alg in prov.algorithms():
                        if 'integral' in alg.id().lower() or 'integral' in alg.displayName().lower():
                            return alg.id()
        except Exception as e:
            QgsMessageLog.logMessage(f"Erro ao procurar sDNA: {e}", "chasm_calculator", Qgis.Critical)
        return None

    def _pick_selected_layers_by_geom(self):
        """
        Pega 1 camada de linhas e 1 de polígonos das camadas selecionadas no painel.
        Retorna (line_layer, polygon_layer). Pode retornar (None, None) se não achar.
        """
        try:
            selected = self.iface.layerTreeView().selectedLayers() or []
        except Exception:
            selected = []
        line_layer = None
        poly_layer = None
        for lyr in selected:
            try:
                gtype = QgsWkbTypes.geometryType(lyr.wkbType())
                if gtype == QgsWkbTypes.LineGeometry and line_layer is None:
                    line_layer = lyr
                elif gtype == QgsWkbTypes.PolygonGeometry and poly_layer is None:
                    poly_layer = lyr
            except Exception:
                continue
        return line_layer, poly_layer

    def _auto_pick_polygon_id_field(self, poly_layer):
        # prioriza cod_setor
        candidate_names = ['cod_setor', 'COD_SETOR', 'cd_setor', 'CD_SETOR', 'id', 'ID', 'gid', 'fid']
        names = [f.name() for f in poly_layer.fields()]
        for c in candidate_names:
            if c in names:
                return c
        return names[0] if names else None

    # ------------------------------ Fragmentação (Processamento) ------------------------------
    def fragment_lines_by_polygons(self, line_layer, poly_layer, poly_id_field,
                            out_field_name="cod_setor",
                            poly_group_interest_field="grupo_interesse",
                            poly_group_others_field="grupo_outros"):
        """
        Fragmenta linhas pelos polígonos e:
        • copia o identificador do setor para as linhas (out_field_name),
        • cria length_m e line_sector_id = "{id}_{out_field_name}",
        • distribui grupos proporcionalmente ao comprimento (g_in_exist / g_ou_exist);
        quando o setor não tiver GI/GO, grava 0.0 (não deixa NULL),
        • sempre escreve cenário não segregado (g_int_ns / g_ou_ns) usando proporção global,
        • escreve nos polígonos a soma por setor (g_in_sum / g_ou_sum),
        • LOGA no painel de mensagens os valores atribuídos por segmento.
        """
        import processing
        from qgis.core import QgsMessageLog, QgsField

        if line_layer is None or poly_layer is None:
            raise RuntimeError("Camadas de linha e polígono são obrigatórias.")
        from qgis.core import QgsWkbTypes, Qgis
        if QgsWkbTypes.geometryType(line_layer.wkbType()) != QgsWkbTypes.LineGeometry:
            raise RuntimeError(f"A camada '{line_layer.name()}' não é de linhas.")
        if QgsWkbTypes.geometryType(poly_layer.wkbType()) != QgsWkbTypes.PolygonGeometry:
            raise RuntimeError(f"A camada '{poly_layer.name()}' não é de polígonos.")
        if poly_id_field not in [f.name() for f in poly_layer.fields()]:
            raise RuntimeError(f"O campo '{poly_id_field}' não existe em '{poly_layer.name()}'.")

        # 1) Corrige geometrias
        fixed_lines = processing.run("native:fixgeometries",
            {"INPUT": line_layer, "OUTPUT": "TEMPORARY_OUTPUT"})["OUTPUT"]
        fixed_polys = processing.run("native:fixgeometries",
            {"INPUT": poly_layer, "OUTPUT": "TEMPORARY_OUTPUT"})["OUTPUT"]

        # 2) Harmoniza CRS
        if fixed_polys.crs() != fixed_lines.crs():
            fixed_polys = processing.run("native:reprojectlayer",
                {"INPUT": fixed_polys, "TARGET_CRS": fixed_lines.crs(), "OUTPUT": "TEMPORARY_OUTPUT"})["OUTPUT"]

        # 3) Interseção trazendo campo de setor
        inter = processing.run("native:intersection", {
            "INPUT": fixed_lines,
            "OVERLAY": fixed_polys,
            "OVERLAY_FIELDS": [poly_id_field],
            "OUTPUT": "TEMPORARY_OUTPUT"
        })["OUTPUT"]

        # 4) Garante campo de setor com nome desejado
        if out_field_name not in [f.name() for f in inter.fields()]:
            inter = processing.run("native:fieldcalculator", {
                "INPUT": inter,
                "FIELD_NAME": out_field_name,
                "FIELD_TYPE": 2, "FIELD_LENGTH": 100,
                "FORMULA": f'"{poly_id_field}"',
                "OUTPUT": "TEMPORARY_OUTPUT"
            })["OUTPUT"]

        # 5) length_m
        with_len = processing.run("native:fieldcalculator", {
            "INPUT": inter,
            "FIELD_NAME": "length_m",
            "FIELD_TYPE": 0, "FIELD_LENGTH": 20, "FIELD_PRECISION": 3,
            "FORMULA": "$length",
            "OUTPUT": "TEMPORARY_OUTPUT"
        })["OUTPUT"]

        # 6) line_sector_id = "{id}_{cod_setor}"
        with_concat = processing.run("native:fieldcalculator", {
            "INPUT": with_len,
            "FIELD_NAME": "line_sector_id",
            "FIELD_TYPE": 2, "FIELD_LENGTH": 100,
            "FORMULA": f"to_string($id) || '_' || \"{out_field_name}\"",
            "OUTPUT": "TEMPORARY_OUTPUT"
        })["OUTPUT"]

        # --- Preparos para distribuição ---
        names_poly = [f.name() for f in fixed_polys.fields()]
        has_gi = poly_group_interest_field in names_poly
        has_go = poly_group_others_field in names_poly

        # Proporção global (para NS)
        gi_total_global = 0.0
        go_total_global = 0.0
        if has_gi or has_go:
            for pf in fixed_polys.getFeatures():
                try:
                    if has_gi:
                        gi_total_global += float(pf[poly_group_interest_field] or 0.0)
                    if has_go:
                        go_total_global += float(pf[poly_group_others_field] or 0.0)
                except Exception:
                    pass
        total_global = gi_total_global + go_total_global
        p_in = (gi_total_global / total_global) if total_global > 0 else 0.0
        p_out = (go_total_global / total_global) if total_global > 0 else 0.0
        QgsMessageLog.logMessage(
            f"[Chasm] Proporção global (NS): p_in={p_in:.6f}, p_out={p_out:.6f} (total_global={total_global})",
            "chasm_calculator", Qgis.Info
        )

        # Mapa setor -> (GI_total, GO_total)
        group_totals = {}
        if has_gi or has_go:
            for pf in fixed_polys.getFeatures():
                sid_key = "" if pf[poly_id_field] is None else str(pf[poly_id_field]).strip()
                gi_v = None
                go_v = None
                if has_gi:
                    try: gi_v = float(pf[poly_group_interest_field]) if pf[poly_group_interest_field] is not None else None
                    except Exception: gi_v = None
                if has_go:
                    try: go_v = float(pf[poly_group_others_field]) if pf[poly_group_others_field] is not None else None
                    except Exception: go_v = None
                group_totals[sid_key] = (gi_v, go_v)

        # Soma de comprimentos por setor
        setor_idx = with_concat.fields().indexOf(out_field_name)
        len_idx   = with_concat.fields().indexOf("length_m")
        sector_len_sum = {}
        for f in with_concat.getFeatures():
            sid_key = "" if f[setor_idx] is None else str(f[setor_idx]).strip()
            try:
                l = float(f[len_idx] or 0.0)
            except Exception:
                l = 0.0
            sector_len_sum[sid_key] = sector_len_sum.get(sid_key, 0.0) + l

        # Campos de saída
        prov = with_concat.dataProvider()
        with_concat.startEditing()
        for nm in ("g_in_exist", "g_ou_exist", "g_int_ns", "g_ou_ns"):
            if with_concat.fields().indexOf(nm) < 0:
                prov.addAttributes([QgsField(nm, QVariant.Double)])
        with_concat.updateFields()

        gi_idx  = with_concat.fields().indexOf("g_in_exist")
        go_idx  = with_concat.fields().indexOf("g_ou_exist")
        gin_idx = with_concat.fields().indexOf("g_int_ns")
        gon_idx = with_concat.fields().indexOf("g_ou_ns")
        lsid_idx = with_concat.fields().indexOf("line_sector_id")

        # Preenche linhas
        total_updates = 0
        feat_count = with_concat.featureCount()
        log_every = 1 if feat_count <= 200 else 100

        if not with_concat.isEditable():
            with_concat.startEditing()

        for i, f in enumerate(with_concat.getFeatures(), start=1):
            sid_key = "" if f[setor_idx] is None else str(f[setor_idx]).strip()
            try:
                l = float(f[len_idx] or 0.0)
            except Exception:
                l = 0.0
            total_len = sector_len_sum.get(sid_key, 0.0)
            frac = (l / total_len) if total_len > 0 else 0.0

            gi_total, go_total = group_totals.get(sid_key, (None, None))

            # EXISTENTE: proporcional ao total do setor (se faltar, grava 0.0)
            gi_val = (gi_total * frac) if (gi_total is not None) else 0.0
            go_val = (go_total * frac) if (go_total is not None) else 0.0

            # NÃO SEGREGADO: sempre grava, usando proporção global
            gi_ns_val = (total_global * frac * p_in) if total_global > 0 else 0.0
            go_ns_val = (total_global * frac * p_out) if total_global > 0 else 0.0

            with_concat.changeAttributeValue(f.id(), gi_idx, gi_val)
            with_concat.changeAttributeValue(f.id(), go_idx, go_val)
            with_concat.changeAttributeValue(f.id(), gin_idx, gi_ns_val)
            with_concat.changeAttributeValue(f.id(), gon_idx, go_ns_val)
            total_updates += 1

            if (i % log_every) == 0 or log_every == 1:
                lsid = f[lsid_idx] if lsid_idx >= 0 else ""
                QgsMessageLog.logMessage(
                    (f"[Chasm] seg_id={f.id()} line_sector_id='{lsid}' setor='{sid_key}' "
                    f"len={l:.3f} sum_len={total_len:.3f} frac={frac:.6f} "
                    f"GI_total={gi_total} GO_total={go_total} -> "
                    f"g_in_exist={gi_val} g_ou_exist={go_val} g_int_ns={gi_ns_val} g_ou_ns={go_ns_val}"),
                    "chasm_calculator", Qgis.Info
                )

        with_concat.commitChanges()
        QgsMessageLog.logMessage(
            f"[Chasm] ✓ Atualizados {total_updates} segmentos (featureCount={feat_count}, log_every={log_every}).",
            "chasm_calculator", Qgis.Success
        )

        # (1d) Soma por setor nos polígonos (a partir de g_in_exist/g_ou_exist das linhas)
        sums_by_sector = {}
        for f in with_concat.getFeatures():
            sid_key = "" if f[setor_idx] is None else str(f[setor_idx]).strip()
            try: gi_f = float(f[gi_idx] or 0.0)
            except Exception: gi_f = 0.0
            try: go_f = float(f[go_idx] or 0.0)
            except Exception: go_f = 0.0
            cur = sums_by_sector.get(sid_key, (0.0, 0.0))
            sums_by_sector[sid_key] = (cur[0] + gi_f, cur[1] + go_f)

        poly_prov = fixed_polys.dataProvider()
        fixed_polys.startEditing()
        if fixed_polys.fields().indexOf("g_in_sum") < 0:
            poly_prov.addAttributes([QgsField("g_in_sum", QVariant.Double)])
        if fixed_polys.fields().indexOf("g_ou_sum") < 0:
            poly_prov.addAttributes([QgsField("g_ou_sum", QVariant.Double)])
        fixed_polys.updateFields()
        gi_s_idx = fixed_polys.fields().indexOf("g_in_sum")
        go_s_idx = fixed_polys.fields().indexOf("g_ou_sum")

        wrote_polys = 0
        for pf in fixed_polys.getFeatures():
            sid_key = "" if pf[poly_id_field] is None else str(pf[poly_id_field]).strip()
            gi_s, go_s = sums_by_sector.get(sid_key, (0.0, 0.0))
            fixed_polys.changeAttributeValue(pf.id(), gi_s_idx, gi_s)
            fixed_polys.changeAttributeValue(pf.id(), go_s_idx, go_s)
            wrote_polys += 2
        fixed_polys.commitChanges()
        QgsMessageLog.logMessage(
            f"[Chasm] Polígonos atualizados com somas (g_in_sum/g_ou_sum): {wrote_polys} alterações.",
            "chasm_calculator", Qgis.Info
        )

        # 9) Adiciona camada resultante
        with_concat.setCrs(fixed_lines.crs())
        with_concat.setName(f"{line_layer.name()}_fragmented_by_{poly_layer.name()}")
        QgsProject.instance().addMapLayer(with_concat)

        # Opcional: refletir os polígonos atualizados
        try:
            poly_name = poly_layer.name()
            QgsProject.instance().removeMapLayer(poly_layer.id())
            fixed_polys.setName(poly_name)
            QgsProject.instance().addMapLayer(fixed_polys)
        except Exception:
            pass

        return with_concat

    # ------------------------------ RUN (fluxo original + sDNA) ------------------------------
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

                # conecta o botão do diálogo para fragmentar
                btn = getattr(self.dlg, "btnFragmentLines", None)
                if btn is not None:
                    btn.clicked.connect(self.do_fragmentation_from_dialog)

            except Exception as e:
                msg = f"Falha ao criar o diálogo ChasmDialog:\n{e}"
                QgsMessageLog.logMessage(msg, "chasm_calculator", Qgis.Critical)
                QMessageBox.critical(self.iface.mainWindow(), "Chasm", msg)
                return

        self.dlg.show()
        if not self.dlg.exec_():
            return

        # ===== A partir daqui é o fluxo normal =====
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

        # (Opcional) Execução sDNA
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

            alg_id = self._sdna_integral_alg_id or _find_sdna_integral_alg()
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
                par = dict(par_base)
                par['destination_weight'] = dw
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

    # ------------------------------ Botão de TESTE ------------------------------
    def do_fragmentation_test(self):
        """
        Usa as camadas SELECIONADAS no painel de camadas:
        - 1ª LINHA + 1º POLÍGONO selecionados
        - Campo do polígono autodetectado (prioriza 'cod_setor')
        - Campos de grupos autodetectados (prioriza 'grupo_interesse' / 'grupo_outros')
        - Executa: fragmentar -> distribuir GI/GO proporcionalmente -> somar por setor.
        """
        line_layer, poly_layer = self._pick_selected_layers_by_geom()
        if not line_layer or not poly_layer:
            self._msg("Selecione no painel ao menos 1 camada de LINHAS e 1 de POLÍGONOS e tente novamente.", Qgis.Warning, 8)
            return

        # Campo ID do setor (ex.: cod_setor)
        poly_id_field = self._auto_pick_polygon_id_field(poly_layer)
        if not poly_id_field:
            self._msg(f"A camada de polígonos '{poly_layer.name()}' não possui campos.", Qgis.Critical, 10)
            return

        # Autodetectar campos de grupos no polígono
        poly_names = [f.name() for f in poly_layer.fields()]
        gi_candidates = ["grupo_interesse", "g_interesse", "gi", "GRUPO_INTERESSE", "GI"]
        go_candidates = ["grupo_outros", "g_outros", "go", "GRUPO_OUTROS", "GO"]

        def pick(cands):
            for c in cands:
                if c in poly_names:
                    return c
            return None

        gi_field = pick(gi_candidates) or "grupo_interesse"
        go_field = pick(go_candidates) or "grupo_outros"

        missing = []
        if gi_field not in poly_names:
            missing.append(gi_field)
        if go_field not in poly_names:
            missing.append(go_field)
        if missing:
            self._msg(
                f"Aviso: campos de grupos ausentes no polígono: {', '.join(missing)}. "
                f"A distribuição proporcional será pulada para o(s) campo(s) ausente(s).",
                Qgis.Warning, 8
            )

        try:
            out = self.fragment_lines_by_polygons(
                line_layer=line_layer,
                poly_layer=poly_layer,
                poly_id_field=poly_id_field,
                out_field_name="cod_setor",
                poly_group_interest_field=gi_field,
                poly_group_others_field=go_field
            )
            self._msg(
                "Concluído (teste):\n"
                "• Linhas: g_in_exist, g_ou_exist (proporcional ao comprimento) + g_int_ns, g_ou_ns.\n"
                "• Polígonos: g_in_sum, g_ou_sum (somados por setor).\n"
                f"Saída de linhas: {out.name()}",
                Qgis.Success, 10
            )
        except Exception as e:
            self._msg(f"Erro (fragmentar+distribuir+somar): {e}", Qgis.Critical, 10)

    # ------------------------------ Botão do DIÁLOGO ------------------------------
    def do_fragmentation_from_dialog(self):
        """
        Usa as escolhas do diálogo (cbPoligonoLayer, cbNetworkLayer e campo ID autodetectado/opcional)
        para fragmentar as linhas pelos polígonos e já preencher as colunas + somar por setor.
        """
        try:
            if self.dlg is None:
                self._msg("Abra o diálogo do Chasm antes de fragmentar.", Qgis.Warning, 8)
                return

            poly_layer_id = getattr(self.dlg, "selected_polygon_layer_id", lambda: None)()
            line_layer_id = getattr(self.dlg, "selected_line_layer_id", lambda: None)()
            poly_id_field = getattr(self.dlg, "selected_polygon_id_field", lambda: None)()

            if not poly_layer_id and hasattr(self.dlg, "cbPoligonoLayer") and self.dlg.cbPoligonoLayer.count():
                poly_layer_id = self.dlg.cbPoligonoLayer.currentData()
            if not line_layer_id and hasattr(self.dlg, "cbNetworkLayer") and self.dlg.cbNetworkLayer.count():
                line_layer_id = self.dlg.cbNetworkLayer.currentData()

            poly_layer = QgsProject.instance().mapLayer(poly_layer_id) if poly_layer_id else None
            line_layer = QgsProject.instance().mapLayer(line_layer_id) if line_layer_id else None

            if line_layer is None:
                self._msg("Selecione a camada de sistema viário (linhas).", Qgis.Warning, 8)
                return
            if poly_layer is None:
                self._msg("Selecione a camada Setores (polígonos).", Qgis.Warning, 8)
                return

            if not poly_id_field:
                poly_id_field = self._auto_pick_polygon_id_field(poly_layer)
                if not poly_id_field:
                    self._msg(f"A camada de polígonos '{poly_layer.name()}' não possui campos.", Qgis.Critical, 10)
                    return

            # Autodetectar campos de grupos
            poly_names = [f.name() for f in poly_layer.fields()]
            gi_candidates = ["grupo_interesse", "g_interesse", "gi", "GRUPO_INTERESSE", "GI"]
            go_candidates = ["grupo_outros", "g_outros", "go", "GRUPO_OUTROS", "GO"]

            def pick(cands):
                for c in cands:
                    if c in poly_names:
                        return c
                return None

            gi_field = pick(gi_candidates) or "grupo_interesse"
            go_field = pick(go_candidates) or "grupo_outros"

            missing = []
            if gi_field not in poly_names:
                missing.append(gi_field)
            if go_field not in poly_names:
                missing.append(go_field)
            if missing:
                self._msg(
                    f"Aviso: campos de grupos ausentes no polígono: {', '.join(missing)}. "
                    f"A distribuição proporcional será pulada para o(s) campo(s) ausente(s).",
                    Qgis.Warning, 8
                )

            out = self.fragment_lines_by_polygons(
                line_layer=line_layer,
                poly_layer=poly_layer,
                poly_id_field=poly_id_field,
                out_field_name="cod_setor",
                poly_group_interest_field=gi_field,
                poly_group_others_field=go_field
            )

            self._msg(
                "Fragmentação concluída:\n"
                "• Linhas: g_in_exist, g_ou_exist (proporcional) + g_int_ns, g_ou_ns.\n"
                "• Polígonos: g_in_sum, g_ou_sum.\n"
                f"Saída: {out.name()}",
                Qgis.Success, 10
            )

        except Exception as e:
            self._msg(f"Erro na fragmentação: {e}", Qgis.Critical, 10)
