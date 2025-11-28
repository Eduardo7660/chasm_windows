# -*- coding: utf-8 -*-
import os

from qgis.PyQt.QtCore import QSettings, QTranslator, QCoreApplication, QVariant
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QMessageBox, QDialogButtonBox
from qgis.core import (
    QgsVectorLayer, QgsProject, QgsMessageLog, Qgis, QgsWkbTypes, QgsApplication,
    QgsField
)

# resources é leve; se faltar, não derruba a classe
try:
    from .resources import *
except Exception:
    pass

# --- sDNA-plus (sdnapy) opcional / import seguro ---
_SDNA_PY_OK = False
_SDNA_PY_VERSION = None
try:
    import sdnapy  # pacote do PyPI "sDNA-plus"
    try:
        from importlib.metadata import version as _pkg_version  # Python 3.8+
        _SDNA_PY_VERSION = _pkg_version("sDNA-plus")
    except Exception:
        _SDNA_PY_VERSION = "unknown"
    _SDNA_PY_OK = True
except Exception:
    sdnapy = None
    _SDNA_PY_OK = False
    _SDNA_PY_VERSION = None


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
        self._ok_running = False  # evita rodar duas vezes no OK

    def tr(self, message):
        return QCoreApplication.translate('Chasm', message)

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

    def _log(self, text, level=Qgis.Info, to_bar=False, duration=6):
        try:
            QgsMessageLog.logMessage(text, "chasm_calculator", level)
        except Exception:
            pass
        if to_bar or level in (Qgis.Warning, Qgis.Critical):
            self._msg(text, level=level, duration=duration)

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
            self._log(f"Erro ao procurar sDNA: {e}", Qgis.Critical, True)
        return None

    def _pick_selected_layers_by_geom(self):
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
        candidate_names = ['cod_setor', 'COD_SETOR', 'cd_setor', 'CD_SETOR', 'id', 'ID', 'gid', 'fid']
        names = [f.name() for f in poly_layer.fields()]
        for c in candidate_names:
            if c in names:
                return c
        return names[0] if names else None

    # ------------------------------ GUI ------------------------------
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

        self.add_action(icon_path, text=self.tr(u'Fragmentar Linhas por Polígonos (teste)'),
                        callback=self.do_fragmentation_test, parent=self.iface.mainWindow())

        try:
            import processing
            self._sdna_integral_alg_id = self._find_sdna_integral_alg()
            self._log(f"initGui: sDNA alg_id cacheado = {self._sdna_integral_alg_id}", Qgis.Info)
        except Exception:
            self._sdna_integral_alg_id = None
            self._log("initGui: provider Processing não disponível no momento.", Qgis.Warning)

        # status do sDNA-plus (sdnapy)
        self._log(f"sDNA-plus (sdnapy) disponível? {_SDNA_PY_OK} v={_SDNA_PY_VERSION}", Qgis.Info)

    def unload(self):
        for action in self.actions:
            self.iface.removePluginMenu(self.tr(u'&Chasm Calculator'), action)
            self.iface.removeToolBarIcon(action)
        if hasattr(self, "translator"):
            try:
                QCoreApplication.removeTranslator(self.translator)
            except Exception:
                pass

    # ---------- Conexões robustas do diálogo (TESTE e OK) ----------
    def _wire_dialog_actions(self):
        """Conecta todos os sinais possíveis ao pipeline e loga o que ficou ligado."""
        wired = []

        # botão "Fragmentar linhas por polígonos" (teste)
        btn_test = getattr(self.dlg, "btnFragmentLines", None)
        if btn_test is not None:
            try:
                btn_test.clicked.disconnect()
            except Exception:
                pass
            btn_test.clicked.connect(self.do_fragmentation_from_dialog)
            wired.append("btnFragmentLines.clicked -> do_fragmentation_from_dialog")

        # botão "OK" custom (se existir no .ui)
        btn_ok_final = getattr(self.dlg, "btnOkFinal", None)
        if btn_ok_final is not None:
            try:
                btn_ok_final.clicked.disconnect()
            except Exception:
                pass
            btn_ok_final.clicked.connect(self._on_ok_clicked)
            wired.append("btnOkFinal.clicked -> _on_ok_clicked")

        # QDialog.accepted (fecha com OK/Enter)
        try:
            self.dlg.accepted.disconnect()
        except Exception:
            pass
        try:
            self.dlg.accepted.connect(self._on_ok_clicked)
            wired.append("dlg.accepted -> _on_ok_clicked")
        except Exception:
            pass

        # buttonBox.accepted (mais comum)
        bb = getattr(self.dlg, "buttonBox", None)
        if bb is not None and isinstance(bb, QDialogButtonBox):
            try:
                bb.accepted.disconnect()
            except Exception:
                pass
            bb.accepted.connect(self._on_ok_clicked)
            wired.append("buttonBox.accepted -> _on_ok_clicked")

            # botão Ok específico (ainda mais garantido)
            try:
                ok_btn = bb.button(QDialogButtonBox.Ok)
                if ok_btn is not None:
                    try:
                        ok_btn.clicked.disconnect()
                    except Exception:
                        pass
                    ok_btn.clicked.connect(self._on_ok_clicked)
                    wired.append("buttonBox.Ok.clicked -> _on_ok_clicked")
            except Exception:
                pass

        self._log("wire_dialog_actions: " + ("; ".join(wired) if wired else "nenhuma conexão feita"), Qgis.Info)

    def _on_ok_clicked(self):
        """Wrapper do clique no OK para logging e anti-duplicação."""
        if self._ok_running:
            self._log("OK clicado (ignorado: já em execução).", Qgis.Warning)
            return
        self._log("OK clicado: iniciando processamento final…", Qgis.Info, True)
        self._ok_running = True
        try:
            self.do_final_from_dialog()
        finally:
            self._ok_running = False

    # --- Resolve dinamicamente IDs de algoritmos do Processing (varia por versão) ---
    def _algo_id(*candidates):
        reg = QgsApplication.processingRegistry()
        for a in candidates:
            try:
                if reg.algorithmById(a):
                    return a
            except Exception:
                pass
        raise RuntimeError(f"Nenhum algoritmo encontrado entre: {candidates}")


    # ------------------------------ Fragmentação (Processamento) ------------------------------
    def fragment_lines_by_polygons(self, line_layer, poly_layer, poly_id_field,
                        out_field_name="cod_setor",
                        poly_group_interest_field="grupo_interesse",
                        poly_group_others_field="grupo_outros"):
        self._log("Etapa 1: iniciando fragmentação...", Qgis.Info, True)
        import processing

        if line_layer is None or poly_layer is None:
            raise RuntimeError("Camadas de linha e polígono são obrigatórias.")
        if QgsWkbTypes.geometryType(line_layer.wkbType()) != QgsWkbTypes.LineGeometry:
            raise RuntimeError(f"A camada '{line_layer.name()}' não é de linhas.")
        if QgsWkbTypes.geometryType(poly_layer.wkbType()) != QgsWkbTypes.PolygonGeometry:
            raise RuntimeError(f"A camada '{poly_layer.name()}' não é de polígonos.")
        if poly_id_field not in [f.name() for f in poly_layer.fields()]:
            raise RuntimeError(f"O campo '{poly_id_field}' não existe em '{poly_layer.name()}'.")

        self._log(f"Etapa 1.1: fix geometries linhas='{line_layer.name()}', polígonos='{poly_layer.name()}'", Qgis.Info)
        fixed_lines = processing.run("native:fixgeometries",
            {"INPUT": line_layer, "OUTPUT": "TEMPORARY_OUTPUT"})["OUTPUT"]
        fixed_polys = processing.run("native:fixgeometries",
            {"INPUT": poly_layer, "OUTPUT": "TEMPORARY_OUTPUT"})["OUTPUT"]

        if fixed_polys.crs() != fixed_lines.crs():
            self._log(f"Etapa 1.2: reproject polígonos -> {fixed_lines.crs().authid()}", Qgis.Info)
            fixed_polys = processing.run("native:reprojectlayer",
                {"INPUT": fixed_polys, "TARGET_CRS": fixed_lines.crs(), "OUTPUT": "TEMPORARY_OUTPUT"})["OUTPUT"]

        self._log("Etapa 1.3: intersection (trazendo id do setor)", Qgis.Info)
        inter = processing.run("native:intersection", {
            "INPUT": fixed_lines,
            "OVERLAY": fixed_polys,
            "OVERLAY_FIELDS": [poly_id_field],
            "OUTPUT": "TEMPORARY_OUTPUT"
        })["OUTPUT"]

        self._log("Etapa 1.4: criando/garantindo campo de setor nas linhas", Qgis.Info)
        if out_field_name not in [f.name() for f in inter.fields()]:
            inter = processing.run("native:fieldcalculator", {
                "INPUT": inter,
                "FIELD_NAME": out_field_name,
                "FIELD_TYPE": 2, "FIELD_LENGTH": 100,
                "FORMULA": f'"{poly_id_field}"',
                "OUTPUT": "TEMPORARY_OUTPUT"
            })["OUTPUT"]

        self._log("Etapa 1.5: calculando length_m", Qgis.Info)
        with_len = processing.run("native:fieldcalculator", {
            "INPUT": inter,
            "FIELD_NAME": "length_m",
            "FIELD_TYPE": 0, "FIELD_LENGTH": 20, "FIELD_PRECISION": 3,
            "FORMULA": "$length",
            "OUTPUT": "TEMPORARY_OUTPUT"
        })["OUTPUT"]

        self._log("Etapa 1.6: criando line_sector_id", Qgis.Info)
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
        self._log(f"Etapa 1.7: campos GI/GO detectados? GI={has_gi} GO={has_go}", Qgis.Info)

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
        self._log(f"Etapa 1.8: proporção global NS -> p_in={p_in:.6f} p_out={p_out:.6f} total={total_global}", Qgis.Info)

        group_totals = {}
        if has_gi or has_go:
            for pf in fixed_polys.getFeatures():
                sid_key = "" if pf[poly_id_field] is None else str(pf[poly_id_field]).strip()
                gi_v = float(pf[poly_group_interest_field]) if has_gi and pf[poly_group_interest_field] is not None else 0.0
                go_v = float(pf[poly_group_others_field])   if has_go and pf[poly_group_others_field]   is not None else 0.0
                group_totals[sid_key] = (gi_v, go_v)

        setor_idx = with_concat.fields().indexOf(out_field_name)
        len_idx   = with_concat.fields().indexOf("length_m")
        sector_len_sum, sector_len_max = {}, {}
        self._log("Etapa 1.9: agregando comprimentos por setor", Qgis.Info)
        for f in with_concat.getFeatures():
            sid_key = "" if f[setor_idx] is None else str(f[setor_idx]).strip()
            try:
                l = float(f[len_idx] or 0.0)
            except Exception:
                l = 0.0
            sector_len_sum[sid_key] = sector_len_sum.get(sid_key, 0.0) + l
            sector_len_max[sid_key] = max(sector_len_max.get(sid_key, 0.0), l)

        prov = with_concat.dataProvider()
        if not with_concat.isEditable():
            with_concat.startEditing()
        for nm in ("g_in_exist", "g_ou_exist", "g_int_ns", "g_ou_ns", "comp_line", "comp_max_setor"):
            if with_concat.fields().indexOf(nm) < 0:
                prov.addAttributes([QgsField(nm, QVariant.Double)])
        with_concat.updateFields()

        gi_idx   = with_concat.fields().indexOf("g_in_exist")
        go_idx   = with_concat.fields().indexOf("g_ou_exist")
        gin_idx  = with_concat.fields().indexOf("g_int_ns")
        gon_idx  = with_concat.fields().indexOf("g_ou_ns")
        cli_idx  = with_concat.fields().indexOf("comp_line")
        cmax_idx = with_concat.fields().indexOf("comp_max_setor")
        lsid_idx = with_concat.fields().indexOf("line_sector_id")

        total_updates = 0
        feat_count = with_concat.featureCount()
        log_every = 1 if feat_count <= 200 else 100
        self._log(f"Etapa 1.10: distribuindo valores por {feat_count} segmentos (log a cada {log_every})", Qgis.Info)

        for i, f in enumerate(with_concat.getFeatures(), start=1):
            sid_key = "" if f[setor_idx] is None else str(f[setor_idx]).strip()
            try:
                l = float(f[len_idx] or 0.0)
            except Exception:
                l = 0.0
            total_len = sector_len_sum.get(sid_key, 0.0)
            frac = (l / total_len) if total_len > 0 else 0.0

            gi_total, go_total = group_totals.get(sid_key, (0.0, 0.0))
            qg_total = gi_total + go_total

            gi_val = (gi_total * frac) if gi_total > 0 else 0.0
            go_val = (go_total * frac) if go_total > 0 else 0.0
            gi_ns_val = (qg_total * frac * p_in)  if qg_total > 0 else 0.0
            go_ns_val = (qg_total * frac * p_out) if qg_total > 0 else 0.0
            max_len_sector = sector_len_max.get(sid_key, 0.0)

            with_concat.changeAttributeValue(f.id(), gi_idx,   gi_val)
            with_concat.changeAttributeValue(f.id(), go_idx,   go_val)
            with_concat.changeAttributeValue(f.id(), gin_idx,  gi_ns_val)
            with_concat.changeAttributeValue(f.id(), gon_idx,  go_ns_val)
            with_concat.changeAttributeValue(f.id(), cli_idx,  l)
            with_concat.changeAttributeValue(f.id(), cmax_idx, max_len_sector)
            total_updates += 1

            if (i % log_every) == 0 or log_every == 1:
                lsid = f[lsid_idx] if lsid_idx >= 0 else ""
                self._log(
                    (f"[Etapa1/seg] id={f.id()} line_sector_id='{lsid}' setor='{sid_key}' "
                     f"len={l:.3f} max_setor={max_len_sector:.3f} sum_len={total_len:.3f} frac={frac:.6f} "
                     f"GI_tot={gi_total} GO_tot={go_total} -> "
                     f"g_in_exist={gi_val} g_ou_exist={go_val} g_int_ns={gi_ns_val} g_ou_ns={go_ns_val}"),
                    Qgis.Info
                )

        with_concat.commitChanges()
        self._log(
            f"Etapa 1.11: ✓ Atualizados {total_updates} segmentos (featureCount={feat_count}).",
            Qgis.Success, True
        )

        # Soma por setor nos polígonos
        self._log("Etapa 1.12: somando g_in_exist/g_ou_exist por setor nos polígonos", Qgis.Info)
        sums_by_sector = {}
        for f in with_concat.getFeatures():
            sid_key = "" if f[setor_idx] is None else str(f[setor_idx]).strip()
            gi_f = float(f[gi_idx] or 0.0)
            go_f = float(f[go_idx] or 0.0)
            cur = sums_by_sector.get(sid_key, (0.0, 0.0))
            sums_by_sector[sid_key] = (cur[0] + gi_f, cur[1] + go_f)

        poly_prov = fixed_polys.dataProvider()
        if not fixed_polys.isEditable():
            fixed_polys.startEditing()
        for nm in ("g_in_sum", "g_ou_sum"):
            if fixed_polys.fields().indexOf(nm) < 0:
                poly_prov.addAttributes([QgsField(nm, QVariant.Double)])
        fixed_polys.updateFields()
        gi_s_idx = fixed_polys.fields().indexOf("g_in_sum")
        go_s_idx = fixed_polys.fields().indexOf("g_ou_sum")

        for pf in fixed_polys.getFeatures():
            sid_key = "" if pf[poly_id_field] is None else str(pf[poly_id_field]).strip()
            gi_s, go_s = sums_by_sector.get(sid_key, (0.0, 0.0))
            fixed_polys.changeAttributeValue(pf.id(), gi_s_idx, gi_s)
            fixed_polys.changeAttributeValue(pf.id(), go_s_idx, go_s)
        fixed_polys.commitChanges()
        self._log("Etapa 1.13: polígonos atualizados com somas.", Qgis.Info)

        # Adiciona camadas ao projeto
        with_concat.setCrs(fixed_lines.crs())
        with_concat.setName(f"{line_layer.name()}_fragmented_by_{poly_layer.name()}")
        QgsProject.instance().addMapLayer(with_concat)
        self._log(f"Etapa 1.14: camada adicionada: {with_concat.name()}", Qgis.Success, True)

        # Reflete polígonos corrigidos (opcional)
        try:
            poly_name = poly_layer.name()
            QgsProject.instance().removeMapLayer(poly_layer.id())
            fixed_polys.setName(poly_name)
            QgsProject.instance().addMapLayer(fixed_polys)
        except Exception:
            pass

        return with_concat

    def _resolve_sdna_param_keys(self, alg_id):
        """
        Descobre dinamicamente as chaves de parâmetros do sDNA Integral da tua build.
        Retorna um dicionário com:
        input_key, output_key,
        destw_key, bet_key, bet_bi_key, junctions_key, hull_key,
        start_gs_key, end_gs_key,
        analmet_key, analmet_options,
        radii_key, bandedradii_key, cont_key,
        weighting_key, weighting_options,
        origweight_key, custommetric_key,
        zonefiles_key, odfile_key, disable_key, oneway_key, intermediates_key, advanced_key
        """
        from qgis.core import QgsApplication
        alg = QgsApplication.processingRegistry().algorithmById(alg_id)
        if alg is None:
            raise RuntimeError(f"Algoritmo '{alg_id}' não disponível.")

        # iniciais
        input_key = output_key = destw_key = None
        bet_key = bet_bi_key = junctions_key = hull_key = None
        start_gs_key = end_gs_key = None
        analmet_key = None
        analmet_options = []
        radii_key = bandedradii_key = cont_key = None
        weighting_key = None
        weighting_options = []
        origweight_key = custommetric_key = None
        zonefiles_key = odfile_key = None
        disable_key = oneway_key = intermediates_key = advanced_key = None

        def lower(s):
            try:
                return (s or "").lower()
            except Exception:
                return ""

        # varre parâmetros
        for p in alg.parameterDefinitions():
            n = p.name()
            ln = lower(n)
            try:
                ld = lower(p.description())
            except Exception:
                ld = ""

            # INPUT / OUTPUT
            if input_key is None and (ln == "input" or "input" in ln or "polyline" in ld or "network" in ld):
                input_key = n

            # DESTINATION WEIGHT
            if destw_key is None and (ln == "destweight" or ("dest" in ln and "weight" in ln) or ("destination" in ld and "weight" in ld)):
                destw_key = n

            # Betweenness / Bidirectional
            if bet_key is None and ("betweenness" in ln):
                bet_key = n
            if bet_bi_key is None and (ln in ("bidir", "bidirectional", "bidirection", "bi") or "bidirectional" in ld):
                bet_bi_key = n

            # Junctions / Hull
            if junctions_key is None and ("junctions" in ln or "junction" in ln or "junc" in ln or "junction" in ld):
                junctions_key = n
            if hull_key is None and ("hull" in ln or "convex" in ln or "hull" in ld or "convex" in ld):
                hull_key = n

            # Grade separation
            if start_gs_key is None and ("start_gs" in ln or "start grade" in ld):
                start_gs_key = n
            if end_gs_key is None and ("end_gs" in ln or "end grade" in ld):
                end_gs_key = n

            # Métrica (enum)
            if analmet_key is None and ("analmet" in ln or ("metric" in ld and "analysis" in ld)):
                analmet_key = n
                try:
                    if hasattr(p, "options") and callable(getattr(p, "options")):
                        analmet_options = list(p.options())
                except Exception:
                    pass

            # Raios + modos
            if radii_key is None and ("radii" in ln or ("radii" in ld) or (ln == "radius" and "string" in ld)):
                radii_key = n
            if bandedradii_key is None and ("bandedradii" in ln or ("band" in ld and "radius" in ld)):
                bandedradii_key = n
            if cont_key is None and (ln == "cont" or ("continuous" in ld)):
                cont_key = n

            # Weighting (enum)
            if weighting_key is None and (ln == "weighting" or ("weighting" in ld)):
                weighting_key = n
                try:
                    if hasattr(p, "options") and callable(getattr(p, "options")):
                        weighting_options = list(p.options())
                except Exception:
                    pass

            # Origin/custom weights
            if origweight_key is None and ("origweight" in ln or ("origin" in ld and "weight" in ld)):
                origweight_key = n
            if custommetric_key is None and ("custommetric" in ln or ("custom metric" in ld)):
                custommetric_key = n

            # Arquivos/flags adicionais
            if zonefiles_key is None and (ln == "zonefiles" or ("zone" in ld and "csv" in ld)):
                zonefiles_key = n
            if odfile_key is None and (ln == "odfile" or ("origin" in ld and "destination" in ld)):
                odfile_key = n
            if disable_key is None and (ln == "disable" or "disable" in ld):
                disable_key = n
            if oneway_key is None and (ln == "oneway" or "one way" in ld):
                oneway_key = n
            if intermediates_key is None and (ln == "intermediates" or "intermediate" in ld):
                intermediates_key = n
            if advanced_key is None and (ln == "advanced" or "advanced config" in ld):
                advanced_key = n

        # OUTPUT
        for o in alg.outputDefinitions():
            on = o.name()
            if output_key is None and "output" in lower(on):
                output_key = on

        return {
            "input_key": input_key,
            "output_key": output_key,
            "destw_key": destw_key,
            "bet_key": bet_key,
            "bet_bi_key": bet_bi_key,
            "junctions_key": junctions_key,
            "hull_key": hull_key,
            "start_gs_key": start_gs_key,
            "end_gs_key": end_gs_key,
            "analmet_key": analmet_key,
            "analmet_options": analmet_options,
            "radii_key": radii_key,
            "bandedradii_key": bandedradii_key,
            "cont_key": cont_key,
            "weighting_key": weighting_key,
            "weighting_options": weighting_options,
            "origweight_key": origweight_key,
            "custommetric_key": custommetric_key,
            "zonefiles_key": zonefiles_key,
            "odfile_key": odfile_key,
            "disable_key": disable_key,
            "oneway_key": oneway_key,
            "intermediates_key": intermediates_key,
            "advanced_key": advanced_key,
        }

    def _introspect_sdna_params(self, alg_id):
        """
        Loga TODOS os parâmetros/saídas do algoritmo sDNA detectado.
        Útil para auditar diferenças entre builds (evita KeyError surpresa).
        """
        from qgis.core import QgsApplication, Qgis
        alg = QgsApplication.processingRegistry().algorithmById(alg_id)
        if alg is None:
            self._log(f"Introspec: algoritmo '{alg_id}' não encontrado.", Qgis.Critical, True)
            return

        try:
            lines = []
            lines.append(f"[Introspec] Alg: {alg.id()} — {alg.displayName()}")
            lines.append("Parâmetros:")
            for p in alg.parameterDefinitions():
                n = p.name()
                cls = p.__class__.__name__
                try:
                    desc = p.description() or ""
                except Exception:
                    desc = ""
                extra = ""
                # tenta listar opções se for enum
                try:
                    if hasattr(p, "options") and callable(getattr(p, "options")):
                        extra = f" options={list(p.options())}"
                    elif hasattr(p, "mOptions"):
                        extra = f" options={list(getattr(p, 'mOptions'))}"
                except Exception:
                    pass
                lines.append(f"  - {n} ({cls}) desc='{desc}'{extra}")
            lines.append("Saídas:")
            for o in alg.outputDefinitions():
                lines.append(f"  - {o.name()} ({o.__class__.__name__})")
            msg = "\n".join(lines)
            self._log(msg, Qgis.Info, True, duration=12)
        except Exception as e:
            self._log(f"Introspec falhou: {e}", Qgis.Warning, True)

    # --- helper: remover arquivos de um SHP base ---
    def _cleanup_shp_bundle(self, path_no_ext: str):
        import os
        for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qmd", ".qpj", ".fix", ".sbn", ".sbx"):
            try:
                p = path_no_ext + ext
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass

    # --- helper: espera o SHP ficar completo e estável ---
    def _wait_for_complete_shp(self, path_no_ext: str, timeout_s: int = 240) -> bool:
        """
        Espera .shp/.shx/.dbf existirem e estabilizarem de tamanho.
        Retorna True se ok; False se timeout.
        """
        import time, os
        start = time.time()

        # 1) esperar aparecer
        while time.time() - start < timeout_s:
            if all(os.path.exists(path_no_ext + ext) for ext in (".shp", ".shx", ".dbf")):
                break
            time.sleep(0.25)
        else:
            return False  # não apareceu

        # 2) estabilizar (tamanho não muda por ~1.5s)
        def sizes():
            try:
                return (
                    os.path.getsize(path_no_ext + ".shp"),
                    os.path.getsize(path_no_ext + ".shx"),
                    os.path.getsize(path_no_ext + ".dbf"),
                )
            except Exception:
                return (-1, -1, -1)

        stable_required = 6  # 6 * 0.25s = 1.5s estável
        stable_ticks = 0
        last = sizes()
        while time.time() - start < timeout_s:
            time.sleep(0.25)
            cur = sizes()
            if cur == last and all(v > 0 for v in cur):
                stable_ticks += 1
                if stable_ticks >= stable_required:
                    return True
            else:
                stable_ticks = 0
                last = cur

        return False

    # ------------------------------ Runner sDNA + JOIN MAD (2a + 2b) ------------------------------
    def _sdna_integral_and_join_mad(self, base_line_layer, sdna_ui_params=None):
        """
        Executa sDNA Integral + JOIN do campo MAD resultante de cada DW.
        Exporta sempre para SHP físico, aguarda escrita estabilizar e usa nomes curtos.
        """
        import os, uuid, time, re, tempfile, shutil, subprocess
        import processing as pr
        from qgis.core import (
            Qgis, QgsProject, QgsApplication, QgsVectorLayer,
            QgsVectorFileWriter, QgsFields, QgsField, QgsFeature, QgsWkbTypes
        )
        from qgis.PyQt.QtCore import QVariant

        self._log("Etapa 2: iniciando sDNA (2a) + JOIN MAD (2b)...", Qgis.Info, True)

        # Binário externo do sDNA (fora do ambiente do QGIS)
        sdna_bin = os.environ.get("CHASM_SDNA_BIN", "sdnaintegral")
        sdna_exe = shutil.which(sdna_bin)
        if not sdna_exe:
            raise RuntimeError(
                f"Binário sDNA '{sdna_bin}' não encontrado no PATH. "
                "Ajuste o PATH ou defina a variável de ambiente CHASM_SDNA_BIN."
            )
        self._log(f"Etapa 2a: usando sDNA externo '{sdna_exe}'", Qgis.Info)

        # ---- parâmetros vindos da UI (com defaults) ----
        metric_val_str     = "ANGULAR"
        radius_val         = 1600
        radius_mode_str    = "band"     # 'band' ou 'continuous'
        bet_val            = False
        bet_bi_val         = False
        weighting_val_raw  = None
        origin_weight_val  = None
        custom_metric_field= None

        dest_weights = ['g_in_exist', 'g_ou_exist', 'g_int_ns', 'g_ou_ns']

        if isinstance(sdna_ui_params, dict):
            metric_val_str  = (sdna_ui_params.get("metric") or "ANGULAR").strip().upper()
            try:
                radius_val = max(0, int(sdna_ui_params.get("radius", 1600)))
            except Exception:
                radius_val = 1600
            radius_mode_str = (sdna_ui_params.get("radius_mode") or "band").strip().lower()
            bet_val         = bool(sdna_ui_params.get("betweenness", False))
            bet_bi_val      = bool(sdna_ui_params.get("betw_bidirectional", False))

            w = (sdna_ui_params.get("weighting") or "").strip()
            weighting_val_raw = w if w else None

            origin_weight_val   = (sdna_ui_params.get("origin_weight") or "").strip() or None
            custom_metric_field = (sdna_ui_params.get("custom_metric_field") or "").strip() or None

            dw_list = [d for d in (sdna_ui_params.get("dest_weights") or []) if isinstance(d, str) and d.strip()]
            if len(dw_list) >= 1:
                dest_weights = (dw_list + ['','','',''])[:4]

        # radii precisa ser string
        radii_str = str(radius_val)

        # Nomes finais para MAD no layer de saída
        dst_map = {
            (dest_weights[0] or 'g_in_exist'): 'mad_int_exist',
            (dest_weights[1] or 'g_ou_exist'): 'mad_out_exist',
            (dest_weights[2] or 'g_int_ns')  : 'mad_int_ns',
            (dest_weights[3] or 'g_ou_ns')   : 'mad_out_ns'
        }
        run_order = [dw for dw in dest_weights if dw]

        self._log(
            "Etapa 2a: parâmetros sDNA → "
            f"metric={metric_val_str}, radii={radii_str}, modo='{radius_mode_str}', "
            f"DW(s)={run_order}",
            Qgis.Info, True
        )

        current = base_line_layer
        results_info = []

        # sanitizador de basename curto
        def _sanitize_basename(name: str, maxlen: int = 24) -> str:
            base = re.sub(r'[^A-Za-z0-9_]+', '_', name or 'layer')
            base = base.strip('_') or 'layer'
            return base[:maxlen] or 'layer'

        def _run_sdna_once(dw_field: str):
            # Sanity
            if current is None or not hasattr(current, "isValid") or not current.isValid():
                raise RuntimeError("Camada de entrada para sDNA está inválida ou None.")
            field_names = [f.name() for f in current.fields()]
            if dw_field not in field_names:
                preview = ", ".join(field_names[:10]) + ("…" if len(field_names) > 10 else "")
                raise RuntimeError(
                    f"Campo de Destination Weight '{dw_field}' não existe na camada '{current.name()}'. "
                    f"Campos disponíveis: {preview}"
                )

            # --- criar uma layer mínima em memória (geom + dw_field) ---
            crs = current.crs()
            wkb = current.wkbType()
            geom_keyword = "MultiLineString" if QgsWkbTypes.isMultiType(wkb) else "LineString"
            mem = QgsVectorLayer(f"{geom_keyword}?crs={crs.authid()}", "sdna_minimal", "memory")
            if not mem.isValid():
                mem = QgsVectorLayer(f"LineString?crs={crs.authid()}", "sdna_minimal", "memory")
                if not mem.isValid():
                    raise RuntimeError("Falha ao criar camada temporária em memória para exportação do sDNA.")

            # Campos: PolyLineId (INT) + DW (Double/String, conforme origem)
            src_idx = current.fields().indexOf(dw_field)
            src_qvar = current.fields()[src_idx].type()

            polylineid_field = QgsField("PolyLineId", QVariant.Int)  # 10 chars, OK p/ SHP e padrão sDNA
            dw_out_field = QgsField(
                dw_field[:10],  # garante <=10 chars p/ SHP
                QVariant.Double if src_qvar in (QVariant.Int, QVariant.LongLong, QVariant.Double) else QVariant.String
            )

            prov = mem.dataProvider()
            prov.addAttributes([polylineid_field, dw_out_field])
            mem.updateFields()

            id_idx = mem.fields().indexOf("PolyLineId")
            dw_idx = mem.fields().indexOf(dw_out_field.name())

            feats = []
            seq_id = 1

            # Se a camada original já tiver um PolyLineId inteiro, preserva; senão, gera sequencial
            orig_names = [f.name() for f in current.fields()]
            orig_has_polyid = "PolyLineId" in orig_names or "polylineid" in [n.lower() for n in orig_names]
            orig_poly_idx = None
            if orig_has_polyid:
                # pega com case exato se existir
                if "PolyLineId" in orig_names:
                    orig_poly_idx = current.fields().indexOf("PolyLineId")
                else:
                    # encontra versão case-insensitive
                    for n in orig_names:
                        if n.lower() == "polylineid":
                            orig_poly_idx = current.fields().indexOf(n)
                            break

            for f in current.getFeatures():
                nf = QgsFeature(mem.fields())
                nf.setGeometry(f.geometry())

                # PolyLineId
                if orig_poly_idx is not None:
                    try:
                        v = f[orig_poly_idx]
                        v_int = int(v) if v is not None else seq_id
                    except Exception:
                        v_int = seq_id
                else:
                    v_int = seq_id
                nf.setAttribute(id_idx, v_int)

                # DW
                val = f[dw_field]
                if dw_out_field.type() == QVariant.Double:
                    try:
                        val = float(val) if val not in (None, "") else 0.0
                    except Exception:
                        val = 0.0
                else:
                    val = "" if val is None else str(val)
                nf.setAttribute(dw_idx, val)

                feats.append(nf)
                seq_id += 1

            prov.addFeatures(feats)
            mem.updateExtents()

            # --- salvar INPUT como SHP (robusto p/ sDNA) ---
            tmp_dir = tempfile.mkdtemp(prefix="chasm_sdna_")
            in_shp_base = f"in_{uuid.uuid4().hex[:6]}"
            in_shp = os.path.join(tmp_dir, f"{in_shp_base}.shp")
            in_no_ext = in_shp[:-4]
            self._cleanup_shp_bundle(in_no_ext)

            # 0) filtra geometrias válidas (linhas não têm área; $area IS NULL é ok)
            mem_no_empty = pr.run("native:extractbyexpression", {
                "INPUT": mem,
                "EXPRESSION": "geometry(@feature) IS NOT NULL AND $length > 0",
                "OUTPUT": "TEMPORARY_OUTPUT"
            })["OUTPUT"]

            # 1) makevalid
            try:
                mk_id = _algo_id("native:makevalid", "qgis:makevalid")
                mem_valid = pr.run(mk_id, {
                    "INPUT": mem_no_empty,
                    "OUTPUT": "TEMPORARY_OUTPUT"
                })["OUTPUT"]
            except Exception:
                mem_valid = mem_no_empty

            # 2) multipart → singlepart
            try:
                mem_single = pr.run("native:multiparttosingleparts", {
                    "INPUT": mem_valid,
                    "OUTPUT": "TEMPORARY_OUTPUT"
                })["OUTPUT"]
            except Exception:
                mem_single = mem_valid

            # 3) drop M/Z
            try:
                mem_2d = pr.run("native:dropmzvalues", {
                    "INPUT": mem_single,
                    "OUTPUT": "TEMPORARY_OUTPUT"
                })["OUTPUT"]
            except Exception:
                mem_2d = mem_single

            # 4) segmentize (opcional)
            try:
                seg_id = _algo_id("native:segmentizebymaxdistance",
                                "native:segmentizebymaxangle",
                                "qgis:densifygeometriesgivenaninterval")  # fallback aproximado
                seg_params = {"INPUT": mem_2d, "OUTPUT": "TEMPORARY_OUTPUT"}

                # define o parâmetro conforme o algoritmo escolhido
                if seg_id.endswith("segmentizebymaxdistance"):
                    # QGIS >= 3.22
                    seg_params["MAX_SEG_LENGTH"] = 0.0
                elif seg_id.endswith("segmentizebymaxangle"):
                    # alternativo por ângulo (radianos)
                    seg_params["MAX_ANGLE"] = 0.0
                else:
                    # qgis:densifygeometriesgivenaninterval (fallback)
                    seg_params["INTERVAL"] = 0.0

                mem_2d_seg = pr.run(seg_id, seg_params)["OUTPUT"]
            except Exception:
                mem_2d_seg = mem_2d

            # 5) garante que nada ficou vazio após transformações
            mem_ready = pr.run("native:extractbyexpression", {
                "INPUT": mem_2d_seg,
                "EXPRESSION": "geometry(@feature) IS NOT NULL",
                "OUTPUT": "TEMPORARY_OUTPUT"
            })["OUTPUT"]

            # Nome final do DW já está truncado em dw_field (≤10)
            dw_field_cli = dw_field

            # 6) salva com 'native:savefeatures' (mais tolerante que QgsVectorFileWriter)
            saved = pr.run("native:savefeatures", {
                "INPUT": mem_ready,
                "OUTPUT": in_shp,
                "LAYER_NAME": "",
                "DATASOURCE_OPTIONS": "ENCODING=UTF-8",
                "LAYER_OPTIONS": "SHPT=ARC;ENCODING=UTF-8"
            })["OUTPUT"]

            # 7) espera o bundle SHP estabilizar
            if not self._wait_for_complete_shp(in_no_ext, timeout_s=90):
                raise RuntimeError(f"SHP de INPUT não ficou completo/estável: {in_shp}")

            # Confirma o nome real do campo DW no SHP (case-insensitive)
            try:
                shp_check = QgsVectorLayer(in_shp, "chk", "ogr")
                field_names_lower = {f.name().lower(): f.name() for f in shp_check.fields()}
                dw_field = field_names_lower.get(dw_field_cli.lower(), dw_field_cli)
            except Exception:
                dw_field = dw_field_cli

            # --- definir OUTPUT como SHP de nome curto ---
            shp_base = f"sdna_{uuid.uuid4().hex[:6]}"
            out_shp  = os.path.join(tmp_dir, f"{shp_base}.shp")
            base_no_ext = out_shp[:-4]
            self._cleanup_shp_bundle(base_no_ext)  # limpa resíduos

            # ---- Executa sDNA via CLI externo (fora do Processing/QGIS)
            opts = [
                ("metric", metric_val_str.lower()),
                ("radii", radii_str),
                ("destweight", dw_field),
            ]
            if radius_mode_str == "continuous":
                opts.append(("cont", "true"))
            if bet_val:
                opts.append(("betweenness", "true"))
                if bet_bi_val:
                    opts.append(("bidir", "true"))
            if weighting_val_raw:
                # CLI do sDNA integral não aceita "weighting"; loga e ignora
                self._log(
                    f"sDNA CLI: parâmetro 'weighting' não suportado; ignorando valor '{weighting_val_raw}'.",
                    Qgis.Warning
                )
            if origin_weight_val:
                opts.append(("origweight", origin_weight_val))
            if metric_val_str == "CUSTOM" and custom_metric_field:
                opts.append(("custommetric", custom_metric_field))

            param_str = ";".join(f"{k}={v}" for k, v in opts if v not in (None, ""))
            cmd = [sdna_exe, "-i", in_shp, "-o", out_shp, param_str]
            self._log(f"Etapa 2a: executando CLI: {' '.join(cmd)}", Qgis.Info)

            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
            except Exception as e:
                raise RuntimeError(f"Falha ao chamar '{sdna_exe}' para DW '{dw_field}': {e}") from e

            stdout_t = (proc.stdout or "").strip()
            stderr_t = (proc.stderr or "").strip()
            if proc.returncode != 0:
                raise RuntimeError(
                    f"sDNA (DW '{dw_field}') retornou código {proc.returncode}. "
                    f"STDOUT: {stdout_t} STDERR: {stderr_t}"
                )
            if stdout_t:
                self._log(f"sDNA stdout (DW={dw_field}): {stdout_t}", Qgis.Info)
            if stderr_t:
                self._log(f"sDNA stderr (DW={dw_field}): {stderr_t}", Qgis.Warning)

            # espera o SHP completo e estável (até 240s)
            if not self._wait_for_complete_shp(base_no_ext, timeout_s=240):
                possible_csv = base_no_ext + ".csv"
                if os.path.exists(possible_csv):
                    self._log(
                        "sDNA gerou CSV em vez de SHP (provável INPUT sem PolyLineId ou incompatibilidade de driver).",
                        Qgis.Warning, True
                    )
                raise RuntimeError(f"sDNA terminou mas o SHP de saída não ficou completo/estável: {out_shp}")

            # sanity: sidecars existem e têm tamanho razoável?
            try:
                side_ok = all(os.path.exists(base_no_ext + ext) for ext in (".shp", ".shx", ".dbf"))
                size_ok = side_ok and all(os.path.getsize(base_no_ext + ext) > 100 for ext in (".shp", ".shx", ".dbf"))
            except Exception:
                side_ok, size_ok = False, False

            # 1) tentativa direta
            out_name = f"{base_line_layer.name()}_sDNA_{dw_field}"
            out_lyr = None
            if side_ok and size_ok:
                out_lyr = QgsVectorLayer(out_shp, out_name, "ogr")
                if not out_lyr or not out_lyr.isValid():
                    # dá mais uma chance (latência de FS)
                    time.sleep(0.5)
                    out_lyr = QgsVectorLayer(out_shp, out_name, "ogr")

            # 2) fallback: converter para GPKG com GDAL e carregar
            if not out_lyr or not out_lyr.isValid():
                try:
                    gpkg_tmp = os.path.join(tmp_dir, f"{shp_base}.gpkg")
                    vt = pr.run("gdal:vectortranslate", {
                        "INPUT": out_shp,
                        "OUTPUT": gpkg_tmp,
                        "LAYER_NAME": "sdna",
                        # opções extras que ajudam shapefiles “enjoados”
                        "OPTIONS": "",
                        "GEOMETRY": "PROMOTE_TO_MULTI",   # garante multilinhas se preciso
                    })
                    out_lyr = QgsVectorLayer(gpkg_tmp, out_name, "ogr")
                except Exception as e:
                    self._log(f"Fallback GPKG falhou: {e}", Qgis.Warning)

            # 3) fallback alternativo: carregar em memória (TEMPORARY_OUTPUT) via vectortranslate
            if not out_lyr or not out_lyr.isValid():
                try:
                    vt_mem = pr.run("gdal:vectortranslate", {
                        "INPUT": out_shp,
                        "OUTPUT": "TEMPORARY_OUTPUT",
                        "LAYER_NAME": "sdna",
                        "GEOMETRY": "PROMOTE_TO_MULTI",
                    })
                    out_lyr = vt_mem["OUTPUT"] if isinstance(vt_mem, dict) else vt_mem
                    if out_lyr and hasattr(out_lyr, "setName"):
                        out_lyr.setName(out_name)
                except Exception as e:
                    self._log(f"Fallback memória falhou: {e}", Qgis.Warning)

            # 4) CSV detectado? Informe claramente o motivo provável
            if (not out_lyr or not out_lyr.isValid()):
                possible_csv = base_no_ext + ".csv"
                if os.path.exists(possible_csv):
                    raise RuntimeError(
                        "sDNA gerou CSV (sem geometria). Verifique se o INPUT tem campo inteiro 'PolyLineId' "
                        "e se o driver de saída está OK no ambiente."
                    )

            # sucesso final?
            if not out_lyr or not out_lyr.isValid():
                raise RuntimeError(f"sDNA terminou, mas a saída não pôde ser carregada (nem com fallbacks): {out_shp}")

            # adiciona e segue
            out_lyr.setName(out_name)
            return out_lyr


        # ===== Loop por DWs =====
        for dw in run_order:
            out_lyr = _run_sdna_once(dw)

            out_lyr.setName(f"{base_line_layer.name()}_sDNA_{dw}")
            QgsProject.instance().addMapLayer(out_lyr)
            self._log(
                f"Etapa 2a: OK DW='{dw}' -> '{out_lyr.name()}' ({out_lyr.featureCount()} feições)",
                Qgis.Success
            )

            # Campo MAD*
            mad_field = None
            for f in out_lyr.fields():
                if f.name().upper().startswith('MAD'):
                    mad_field = f.name(); break
            if not mad_field:
                raise RuntimeError(f"Campo MAD* não encontrado na saída do sDNA para DW '{dw}'.")

            # 2b — JOIN MAD (equals -> fallback intersects)
            try:
                joined = pr.run("native:joinattributesbylocation", {
                    "INPUT": current, "JOIN": out_lyr,
                    "PREDICATE": [5],  # equals
                    "JOIN_FIELDS": [mad_field],
                    "METHOD": 0, "DISCARD_NONMATCHING": False,
                    "PREFIX": "", "OUTPUT": "TEMPORARY_OUTPUT"
                })["OUTPUT"]
            except Exception:
                joined = pr.run("native:joinattributesbylocation", {
                    "INPUT": current, "JOIN": out_lyr,
                    "PREDICATE": [0],  # intersects
                    "JOIN_FIELDS": [mad_field],
                    "METHOD": 0, "DISCARD_NONMATCHING": False,
                    "PREFIX": "", "OUTPUT": "TEMPORARY_OUTPUT"
                })["OUTPUT"]

            target_name = {
                (dest_weights[0] or 'g_in_exist'): 'mad_int_exist',
                (dest_weights[1] or 'g_ou_exist'): 'mad_out_exist',
                (dest_weights[2] or 'g_int_ns')  : 'mad_int_ns',
                (dest_weights[3] or 'g_ou_ns')   : 'mad_out_ns'
            }.get(dw, f"mad_{dw}".replace(" ", "_").lower()[:30])

            with_new = pr.run("native:fieldcalculator", {
                "INPUT": joined, "FIELD_NAME": target_name,
                "FIELD_TYPE": 0, "FIELD_LENGTH": 20, "FIELD_PRECISION": 6,
                "FORMULA": f"\"{mad_field}\"", "OUTPUT": "TEMPORARY_OUTPUT"
            })["OUTPUT"]
            cleaned = pr.run("native:deletecolumn", {
                "INPUT": with_new, "COLUMN": [mad_field], "OUTPUT": "TEMPORARY_OUTPUT"
            })["OUTPUT"]

            current = cleaned
            results_info.append((dw, out_lyr.name(), mad_field, target_name))
            self._log(f"Etapa 2b: JOIN MAD concluído (DW='{dw}')", Qgis.Success)

        current.setName(f"{base_line_layer.name()}_with_MAD")
        QgsProject.instance().addMapLayer(current)
        for dw, out_name, mad_src, mad_dst in results_info:
            self._log(f"JOIN MAD resumo: DW={dw} saída='{out_name}' {mad_src} -> {mad_dst}", Qgis.Info)
        self._log(f"Etapa 2: concluída — camada final '{current.name()}'", Qgis.Success, True)
        return current

    # ------------------------------ RUN: abre diálogo e liga sinais ------------------------------
    def run(self):
        self._log("run(): abrindo diálogo...", Qgis.Info)
        if self.dlg is None:
            try:
                from .chasm_calculator_dialog import ChasmDialog
            except Exception as e:
                msg = f"Falha ao importar chasm_calculator_dialog:\n{e}"
                self._log(msg, Qgis.Critical, True)
                QMessageBox.critical(self.iface.mainWindow(), "Chasm", msg)
                return
            try:
                self.dlg = ChasmDialog(self.iface.mainWindow())
            except Exception as e:
                msg = f"Falha ao criar o diálogo ChasmDialog:\n{e}"
                self._log(msg, Qgis.Critical, True)
                QMessageBox.critical(self.iface.mainWindow(), "Chasm", msg)
                return

        # (re)liga sinais sempre que abrir (caso o .ui tenha sido recarregado)
        self._wire_dialog_actions()

        self.dlg.show()
        self.dlg.raise_()
        self.dlg.activateWindow()
        self._log("run(): diálogo exibido.", Qgis.Info)

    # ------------------------------ Botão OK/Final (MESMO pipeline do teste) ------------------------------
    def do_final_from_dialog(self):
        """Executa Etapa 1 + Etapa 2 com as escolhas do diálogo (mesmo pipeline do TESTE)."""
        self._log("OK/Final: iniciando pipeline completo (Etapa 1 + Etapa 2)...", Qgis.Info, True)
        try:
            # 1) Ler camadas do diálogo; se faltar, fallback para 1ª de cada tipo
            poly_layer_id = getattr(self.dlg, "selected_polygon_layer_id", lambda: None)()
            line_layer_id = getattr(self.dlg, "selected_line_layer_id", lambda: None)()
            if not poly_layer_id and hasattr(self.dlg, "cbPoligonoLayer") and self.dlg.cbPoligonoLayer.count():
                poly_layer_id = self.dlg.cbPoligonoLayer.currentData()
            if not line_layer_id and hasattr(self.dlg, "cbNetworkLayer") and self.dlg.cbNetworkLayer.count():
                line_layer_id = self.dlg.cbNetworkLayer.currentData()

            poly_layer = QgsProject.instance().mapLayer(poly_layer_id) if poly_layer_id else None
            line_layer = QgsProject.instance().mapLayer(line_layer_id) if line_layer_id else None

            if line_layer is None or poly_layer is None:
                self._log("OK/Final: fallback para primeira LINHA e POLÍGONO do projeto.", Qgis.Info)
                for lyr in QgsProject.instance().mapLayers().values():
                    try:
                        gtype = QgsWkbTypes.geometryType(lyr.wkbType())
                        if line_layer is None and gtype == QgsWkbTypes.LineGeometry:
                            line_layer = lyr
                        elif poly_layer is None and gtype == QgsWkbTypes.PolygonGeometry:
                            poly_layer = lyr
                        if line_layer and poly_layer:
                            break
                    except Exception:
                        continue

            if line_layer is None or poly_layer is None:
                self._log("OK/Final: não há LINHAS e/ou POLÍGONOS no projeto.", Qgis.Warning, True)
                self._msg("Selecione/mantenha no projeto 1 camada de LINHAS e 1 de POLÍGONOS.", Qgis.Warning, 8)
                return

            # 2) Campo ID
            poly_id_field = getattr(self.dlg, "selected_polygon_id_field", lambda: None)()
            if not poly_id_field:
                poly_id_field = self._auto_pick_polygon_id_field(poly_layer)
            if not poly_id_field:
                self._log("OK/Final: nenhum campo de ID encontrado nos polígonos.", Qgis.Critical, True)
                self._msg(f"A camada de polígonos '{poly_layer.name()}' não possui campos.", Qgis.Critical, 10)
                return

            self._log(f"OK/Final: usando line='{line_layer.name()}', poly='{poly_layer.name()}', id='{poly_id_field}'", Qgis.Info)

            # 3) GI/GO
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
            if gi_field not in poly_names: missing.append(gi_field)
            if go_field not in poly_names: missing.append(go_field)
            if missing:
                self._log(f"OK/Final: campos GI/GO ausentes: {', '.join(missing)} (serão pulados).", Qgis.Warning, True)
                self._msg(f"Aviso: campos ausentes: {', '.join(missing)}. Pulando distribuição para eles.", Qgis.Warning, 8)

            # 4) ETAPA 1
            self._log("OK/Final: chamando fragment_lines_by_polygons(...) (Etapa 1)", Qgis.Info)
            out = self.fragment_lines_by_polygons(
                line_layer=line_layer,
                poly_layer=poly_layer,
                poly_id_field=poly_id_field,
                out_field_name="cod_setor",
                poly_group_interest_field=gi_field,
                poly_group_others_field=go_field
            )
            self._log(f"OK/Final: Etapa 1 concluída -> '{out.name()}' ({out.featureCount()} feições)", Qgis.Success, True)
            self._msg("Etapa 1 concluída.", Qgis.Success, 5)

            # 5) ETAPA 2 (lê parâmetros da UI)
            sdna_params = None
            try:
                sdna_params = self.dlg.sdna_params()
            except Exception:
                pass

            self._log("OK/Final: iniciando Etapa 2 (sDNA + JOIN MAD)...", Qgis.Info, True)
            enriched = self._sdna_integral_and_join_mad(out, sdna_ui_params=sdna_params)
            self._log(
                f"OK/Final: Etapa 2 concluída -> '{enriched.name()}' com campos MAD.",
                Qgis.Success, True
            )
            self._msg(
                f"Etapa 2 concluída: '{enriched.name()}' com MADs adicionados.",
                Qgis.Success, 8
            )

        except Exception as e:
            self._log(f"OK/Final: Erro no processamento final: {e}", Qgis.Critical, True)
            self._msg(f"Erro no processamento final: {e}", Qgis.Critical, 10)

    # ------------------------------ Botão TESTE ------------------------------
    def do_fragmentation_test(self):
        self._log("TESTE: iniciando pipeline completo com camadas SELECIONADAS...", Qgis.Info, True)
        line_layer, poly_layer = self._pick_selected_layers_by_geom()
        if not line_layer or not poly_layer:
            self._log("TESTE: selecione no painel 1 LINHA e 1 POLÍGONO e tente novamente.", Qgis.Warning, True)
            self._msg("Selecione no painel ao menos 1 camada de LINHAS e 1 de POLÍGONOS e tente novamente.", Qgis.Warning, 8)
            return

        poly_id_field = self._auto_pick_polygon_id_field(poly_layer)
        if not poly_id_field:
            self._log(f"TESTE: a camada '{poly_layer.name()}' não possui campos.", Qgis.Critical, True)
            self._msg(f"A camada de polígonos '{poly_layer.name()}' não possui campos.", Qgis.Critical, 10)
            return

        # GI/GO
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
        if gi_field not in poly_names: missing.append(gi_field)
        if go_field not in poly_names: missing.append(go_field)
        if missing:
            self._log(f"TESTE: campos GI/GO ausentes: {', '.join(missing)} (serão pulados).", Qgis.Warning, True)
            self._msg(
                f"Aviso: campos de grupos ausentes no polígono: {', '.join(missing)}. "
                f"A distribuição proporcional será pulada para o(s) campo(s) ausente(s).",
                Qgis.Warning, 8
            )

        try:
            self._log("TESTE: chamando fragment_lines_by_polygons(...) (Etapa 1)", Qgis.Info)
            out = self.fragment_lines_by_polygons(
                line_layer=line_layer,
                poly_layer=poly_layer,
                poly_id_field=poly_id_field,
                out_field_name="cod_setor",
                poly_group_interest_field=gi_field,
                poly_group_others_field=go_field
            )
            self._log(f"TESTE: Etapa 1 concluída -> '{out.name()}' ({out.featureCount()} feições)", Qgis.Success, True)

            # parâmetros da UI (se diálogo estiver aberto)
            sdna_params = None
            try:
                if self.dlg is not None:
                    sdna_params = self.dlg.sdna_params()
            except Exception:
                pass

            self._log("TESTE: iniciando Etapa 2 (sDNA + JOIN MAD)...", Qgis.Info)
            enriched = self._sdna_integral_and_join_mad(out, sdna_ui_params=sdna_params)
            self._log(
                f"TESTE: Etapa 2 concluída -> '{enriched.name()}' com MADs.",
                Qgis.Success, True
            )

            self._msg(
                f"Concluído (Teste): '{enriched.name()}' com MADs adicionados.",
                Qgis.Success, 10
            )

        except Exception as e:
            self._log(f"TESTE: Erro durante o processo: {e}", Qgis.Critical, True)
            self._msg(f"Erro durante o processo completo: {e}", Qgis.Critical, 10)

    # ------------------------------ Botão do DIÁLOGO (fragmentação simples) ------------------------------
    def do_fragmentation_from_dialog(self):
        self._log("Dialog-Fragment: iniciando pipeline com escolhas do diálogo...", Qgis.Info, True)
        try:
            if self.dlg is None:
                self._log("Dialog-Fragment: diálogo não está aberto.", Qgis.Warning, True)
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

            if line_layer is None or poly_layer is None:
                self._log("Dialog-Fragment: faltam camadas no diálogo.", Qgis.Warning, True)
                self._msg("Selecione a camada de sistema viário (linhas) e a de setores (polígonos).", Qgis.Warning, 8)
                return

            if not poly_id_field:
                poly_id_field = self._auto_pick_polygon_id_field(poly_layer)
                if not poly_id_field:
                    self._log("Dialog-Fragment: camada de polígonos sem campos.", Qgis.Critical, True)
                    self._msg(f"A camada de polígonos '{poly_layer.name()}' não possui campos.", Qgis.Critical, 10)
                    return

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
            if gi_field not in poly_names: missing.append(gi_field)
            if go_field not in poly_names: missing.append(go_field)
            if missing:
                self._log(f"Dialog-Fragment: campos ausentes: {', '.join(missing)} (pulando).", Qgis.Warning)

            self._log("Dialog-Fragment: chamando fragment_lines_by_polygons(...) (Etapa 1)", Qgis.Info)
            out = self.fragment_lines_by_polygons(
                line_layer=line_layer,
                poly_layer=poly_layer,
                poly_id_field=poly_id_field,
                out_field_name="cod_setor",
                poly_group_interest_field=gi_field,
                poly_group_others_field=go_field
            )

            self._log(f"Dialog-Fragment: Etapa 1 concluída -> '{out.name()}' ({out.featureCount()} feições)", Qgis.Success, True)

            # parâmetros da UI
            sdna_params = None
            try:
                sdna_params = self.dlg.sdna_params()
            except Exception:
                pass

            self._log("Dialog-Fragment: iniciando Etapa 2 (sDNA + JOIN MAD)...", Qgis.Info)
            enriched = self._sdna_integral_and_join_mad(out, sdna_ui_params=sdna_params)
            self._log(
                f"Dialog-Fragment: Etapa 2 concluída -> '{enriched.name()}' com MADs.",
                Qgis.Success, True
            )

            self._msg(
                f"Fragmentação concluída + sDNA/JOIN: '{enriched.name()}'.",
                Qgis.Success, 10
            )

        except Exception as e:
            self._log(f"Dialog-Fragment: Erro na fragmentação: {e}", Qgis.Critical, True)
            self._msg(f"Erro na fragmentação: {e}", Qgis.Critical, 10)
