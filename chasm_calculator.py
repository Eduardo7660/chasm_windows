# -*- coding: utf-8 -*-
import os

from qgis.PyQt.QtCore import QSettings, QTranslator, QCoreApplication, QVariant
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QMessageBox, QDialogButtonBox
from qgis.core import (
    QgsVectorLayer, QgsProject, QgsMessageLog, Qgis, QgsWkbTypes, QgsApplication,
    QgsField, QgsProcessingFeedback
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

class ProcessingFeedback(QgsProcessingFeedback):
    """
    Uma classe de feedback customizada que captura logs de algoritmos do Processing
    e os envia para o painel de logs do QGIS.
    """
    def __init__(self, logger_func, prefix=""):
        super().__init__()
        self.logger = logger_func
        self.prefix = prefix

    def setProgress(self, progress):
        # Poderíamos usar isso para uma barra de progresso no futuro
        super().setProgress(progress)

    def pushInfo(self, info):
        self.logger(f"{self.prefix} INFO: {info}", Qgis.Info)

    def pushWarning(self, warning):
        self.logger(f"{self.prefix} WARN: {warning}", Qgis.Warning)

    def pushError(self, error):
        self.logger(f"{self.prefix} ERROR: {error}", Qgis.Critical)


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
            # self.dlg.accept() # Fecha o diálogo após iniciar a tarefa
        finally:
            self._ok_running = False

    # --- Resolve dinamicamente IDs de algoritmos do Processing (varia por versão) ---
    def _algo_id(self, *candidates):
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
        
        def clean_filename(name: str) -> str:
            """
            Remove caracteres inválidos e converte tudo para um formato seguro de SHP.
            - Remove acentos
            - Remove caracteres especiais
            - Converte tudo para _
            - Remove múltiplos __
            - Garante tamanho <= 60 (limite seguro para o driver SHP)
            """
            import unicodedata, re

            # remover acentos
            nfkd = unicodedata.normalize("NFKD", name)
            name = "".join([c for c in nfkd if not unicodedata.combining(c)])

            # trocar tudo que não é letra/número por _
            name = re.sub(r"[^A-Za-z0-9]+", "_", name)

            # remover múltiplos __
            name = re.sub(r"_+", "_", name)

            # remover _ no início/fim
            name = name.strip("_")

            # manter tamanho seguro
            if len(name) > 60:
                name = name[:60]

            return name
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

        # --- Deixa a camada pronta para sDNA: single-part, 2D (sem M/Z) e com geometrias corrigidas ---
        self._log(
            "Etapa 1.14: preparando camada resultante para sDNA "
            "(multipart→single, drop M/Z, fix geometries)...",
            Qgis.Info, True
        )

        import processing as pr, os, uuid

        # 1) multipart → singlepart
        lyr_proc = pr.run("native:multiparttosingleparts", {
            "INPUT": with_concat,
            "OUTPUT": "TEMPORARY_OUTPUT"
        })["OUTPUT"]

        # 2) remover M/Z (fica só 2D)
        lyr_proc = pr.run("native:dropmzvalues", {
            "INPUT": lyr_proc,
            "DROP_M_VALUES": True,
            "DROP_Z_VALUES": True,
            "OUTPUT": "TEMPORARY_OUTPUT"
        })["OUTPUT"]

        # 3) fix geometries
        lyr_proc = pr.run("native:fixgeometries", {
            "INPUT": lyr_proc,
            "OUTPUT": "TEMPORARY_OUTPUT"
        })["OUTPUT"]

        # agora essa é a camada “oficial” da Etapa 1
        with_concat = lyr_proc
        with_concat.setCrs(fixed_lines.crs())

        # --- Salvar em disco como SHP em C:\chasm ---
        base_dir = r"C:\chasm"
        if not os.path.exists(base_dir):
            os.makedirs(base_dir, exist_ok=True)

        uid = uuid.uuid4().hex[:6]
        out_shp_path = os.path.join(
            base_dir,
            f"{line_layer.name()}_fragmented_by_{poly_layer.name()}_{uid}.shp"
        )

        self._log(
            f"Etapa 1.15: salvando camada final (pronta para sDNA) em '{out_shp_path}'",
            Qgis.Info, True
        )

        pr.run("native:savefeatures", {
            "INPUT": with_concat,
            "OUTPUT": out_shp_path
        })

        # Carregar do disco
        raw_name = f"{line_layer.name()}_fragmented_by_{poly_layer.name()}"
        safe_name = clean_filename(raw_name)

        # Caminho final do SHP
        out_shp = f"C:\\chasm\\{safe_name}.shp"

        self._log(f"Etapa 1.16: salvando camada final como SHP limpo: {out_shp}", Qgis.Info)

        # Exportar o SHP final
        pr.run("native:savefeatures", {
            "INPUT": with_concat,
            "OUTPUT": out_shp
        })

        # Carregar camada SHP salva
        final_layer = QgsVectorLayer(out_shp, safe_name, "ogr")

        if not final_layer.isValid():
            raise RuntimeError("Falha ao carregar SHP limpo salvo para sDNA.")

        QgsProject.instance().addMapLayer(final_layer)

        self._log(f"Etapa 1.17: SHP salvo e carregado: {safe_name}", Qgis.Success, True)

        return final_layer

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
            self._log(f"Introspec: algoritmo '{alg_id}' não encontrado.", Qgis.Critical, True)
            return

        # iniciais
        input_key = output_key = destw_key = None
        bet_key = bet_bi_key = junctions_key = hull_key = None
        start_gs_key = end_gs_key = None
        analmet_key = None
        radmet_key = None
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

            if radmet_key is None and (ln == "radmet" or "radmet" in ln or "radmet" in ld or "radius metric" in ld):
                radmet_key = n

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
            "radmet_key": radmet_key,
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

    # ------------------------------ Runner sDNA + JOIN MAD (2a + 2b) ------------------------------
    def _sdna_integral_and_join_mad(self, base_line_layer, sdna_ui_params=None):
        """
        Executa sDNA Integral + JOIN do campo MAD resultante de cada DW.
        Exporta sempre para SHP físico de entrada (C:\chasm), aguarda escrita estabilizar
        e deixa o provider do sDNA decidir o caminho de saída (TEMPORARY_OUTPUT).
        """
        import os, uuid, time, re, tempfile, shutil, subprocess
        import processing as pr
        from qgis.core import (
            Qgis, QgsProject, QgsApplication, QgsVectorLayer,
            QgsVectorFileWriter, QgsFields, QgsField, QgsFeature, QgsWkbTypes
        )
        from qgis.PyQt.QtCore import QVariant

        self._log("Etapa 2: iniciando sDNA (2a) + JOIN MAD (2b)...", Qgis.Info, True)

        # === Descobre ID do algoritmo sDNA Integral ===
        alg_id = self._sdna_integral_alg_id or self._find_sdna_integral_alg()
        if not alg_id:
            raise RuntimeError("Algoritmo sDNA Integral não encontrado no Processing.")

        # pega defaults reais do algoritmo (incluindo radmet)
        alg = QgsApplication.processingRegistry().algorithmById(alg_id)
        defaults_map = {}
        if alg is not None:
            try:
                for p in alg.parameterDefinitions():
                    try:
                        defaults_map[p.name()] = p.defaultValue()
                    except Exception:
                        pass
            except Exception:
                pass

        # === Descobre chaves do algoritmo (dinâmico) ===
        keys = self._resolve_sdna_param_keys(alg_id)

        # Extrai mapeamentos
        input_key        = keys.get("input_key")
        output_key       = keys.get("output_key")
        destw_key        = keys.get("destw_key")
        bet_key          = keys.get("bet_key")
        bet_bi_key       = keys.get("bet_bi_key")
        junctions_key    = keys.get("junctions_key")
        hull_key         = keys.get("hull_key")
        start_gs_key     = keys.get("start_gs_key")
        end_gs_key       = keys.get("end_gs_key")
        analmet_key      = keys.get("analmet_key")
        analmet_options  = keys.get("analmet_options")
        radii_key        = keys.get("radii_key")
        radmet_key       = keys.get("radmet_key")

        bandedradii_key  = keys.get("bandedradii_key")
        cont_key         = keys.get("cont_key")
        weighting_key    = keys.get("weighting_key")
        weighting_options= keys.get("weighting_options")
        origweight_key   = keys.get("origweight_key")
        custommetric_key = keys.get("custommetric_key")
        zonefiles_key    = keys.get("zonefiles_key")
        odfile_key       = keys.get("odfile_key")
        disable_key      = keys.get("disable_key")
        oneway_key       = keys.get("oneway_key")
        intermediates_key= keys.get("intermediates_key")
        advanced_key     = keys.get("advanced_key")

        # Sanidade mínima
        if not input_key or not destw_key:
            raise RuntimeError(
                "Não foi possível mapear parâmetros essenciais do sDNA "
                f"(INPUT='{input_key}', DESTW='{destw_key}')."
            )
        if not analmet_key:
            raise RuntimeError("Parâmetro de métrica 'analmet' não encontrado nesta build do sDNA.")

        # ---- parâmetros vindos da UI (com defaults) ----
        metric_val_str     = "ANGULAR"
        radius_val         = 1600
        radius_mode_str    = "band"     # 'band' ou 'continuous'
        bet_val            = False
        bet_bi_val         = False
        weighting_val_raw  = None
        origin_weight_val  = None
        custom_metric_field= None

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
                dest_weights = (dw_list + ["", "", "", ""])[:4]
            else:
                dest_weights = ["g_in_exist", "g_ou_exist", "g_int_ns", "g_ou_ns"]

        # Helpers de enum → índice
        def enum_index(value_str, options, default_idx=None):
            if options is None or len(options) == 0:
                return default_idx
            v = (value_str or "").strip().upper()
            for i, opt in enumerate(options):
                if str(opt).strip().upper() == v:
                    return i
            for i, opt in enumerate(options):
                s = str(opt).strip().upper()
                if s.startswith(v) or v.startswith(s):
                    return i
            return default_idx

        # analmet -> índice
        analmet_idx = enum_index(metric_val_str, analmet_options, default_idx=0)
        if analmet_options and metric_val_str == "CUSTOM" and not custom_metric_field:
            self._log("sDNA: 'CUSTOM' sem campo custommetric — fallback para 'ANGULAR'.", Qgis.Warning, True)
            analmet_idx = enum_index("ANGULAR", analmet_options, default_idx=0)

        # weighting -> índice (se enum)
        def normalize_weighting(val_raw, options):
            if not val_raw:
                return None
            if options and isinstance(options, (list, tuple)):
                return enum_index(val_raw, options, default_idx=None)
            return val_raw

        weighting_val = normalize_weighting(weighting_val_raw, weighting_options)

        banded_val = (radius_mode_str == "band")
        cont_val   = (radius_mode_str == "continuous")
        # radii precisa ser string. Se for 'banded', o sDNA espera um 0 no início.
        radii_str = str(radius_val)
        if banded_val:
            radii_str = f"0,{radii_str}"

        # Nomes finais para MAD no layer de saída
        dst_map = {
            (dest_weights[0] or "g_in_exist"): "mad_int_exist",
            (dest_weights[1] or "g_ou_exist"): "mad_out_exist",
            (dest_weights[2] or "g_int_ns")  : "mad_int_ns",
            (dest_weights[3] or "g_ou_ns")   : "mad_out_ns"
        }
        run_order = [dw for dw in dest_weights if dw]

        self._log(
            "Etapa 2a: mapa sDNA → "
            f"INPUT='{input_key}', DESTW='{destw_key}', OUTPUT='{output_key}', "
            f"ANALMET idx={analmet_idx}, RADII='{radii_key}', WEIGHTING='{weighting_key}'",
            Qgis.Info, True
        )

        current = base_line_layer
        results_info = []

        def _run_sdna_once(dw_field: str):
            """
            Roda o sDNA Integral via *Processing Provider* (sDNA Provider:Integral Analysis).
            Cria INPUT em C:\\chasm, e deixa o provider criar a saída (TEMPORARY_OUTPUT).
            """
            import os, uuid, re
            import processing as pr
            from qgis.core import QgsVectorLayer, QgsProcessingFeedback

            # --- 0) Garantir que C:\chasm existe ---
            base_dir = r"C:\chasm"
            if not os.path.exists(base_dir):
                os.makedirs(base_dir, exist_ok=True)

            # --- 1) Sanidade da camada e do campo DW ---
            if current is None or not hasattr(current, "isValid") or not current.isValid():
                raise RuntimeError("Camada de entrada para sDNA está inválida ou None.")

            field_names = [f.name() for f in current.fields()]
            if dw_field not in field_names:
                preview = ", ".join(field_names[:10]) + ("…" if len(field_names) > 10 else "")
                raise RuntimeError(
                    f"Campo de Destination Weight '{dw_field}' não existe na camada '{current.name()}'. "
                    f"Campos disponíveis: {preview}"
                )

            # --- 2) Single-part + fix geometries ---
            self._log("sDNA Prep: convertendo para single-part + fix geometries", Qgis.Info)
            mem_singlepart = pr.run("native:multiparttosingleparts", {
                "INPUT": current,
                "OUTPUT": "TEMPORARY_OUTPUT"
            })["OUTPUT"]

            mem_clean = pr.run("native:fixgeometries", {
                "INPUT": mem_singlepart,
                "OUTPUT": "TEMPORARY_OUTPUT"
            })["OUTPUT"]

            # --- 3) Criar SHP de entrada em disco (2 passos: DW seguro + PolyLineId) ---
            uid = uuid.uuid4().hex[:6]

            in_shp_1 = os.path.join(base_dir, f"in_{dw_field}_{uid}.shp")
            in_shp_2 = os.path.join(base_dir, f"in_{dw_field}_{uid}_final.shp")

            # nome de campo de peso seguro (<=10 chars, só letras/números/_)
            safe_dw = re.sub(r"[^A-Za-z0-9_]+", "_", dw_field)[:10] or "DW"

            self._log(f"sDNA Prep (1/3): criando SHP com campo de peso '{safe_dw}' em '{in_shp_1}'", Qgis.Info)
            res1 = pr.run("native:fieldcalculator", {
                "INPUT": mem_clean,
                "FIELD_NAME": safe_dw,
                "FIELD_TYPE": 0,  # Float
                "FIELD_LENGTH": 20,
                "FIELD_PRECISION": 10,
                "FORMULA": f"coalesce(\"{dw_field}\", 0)",
                "OUTPUT": in_shp_1
            })

            if not os.path.exists(in_shp_1):
                try:
                    listing = ", ".join(os.listdir(base_dir))
                except Exception:
                    listing = "NÃO FOI POSSÍVEL LISTAR"
                raise RuntimeError(
                    f"Falha ao criar SHP de entrada (passo 1): {in_shp_1}. "
                    f"Arquivos em C:\\chasm: {listing}"
                )

            self._log(f"sDNA Prep (2/3): adicionando PolyLineId em '{in_shp_2}'", Qgis.Info)
            res2 = pr.run("native:fieldcalculator", {
                "INPUT": res1["OUTPUT"],
                "FIELD_NAME": "PolyLineId",
                "FIELD_TYPE": 1,  # Integer
                "FIELD_LENGTH": 10,
                "FIELD_PRECISION": 0,
                "FORMULA": "@row_number",
                "OUTPUT": in_shp_2
            })

            in_shp = in_shp_2
            if not os.path.exists(in_shp):
                try:
                    listing = ", ".join(os.listdir(base_dir))
                except Exception:
                    listing = "NÃO FOI POSSÍVEL LISTAR"
                raise RuntimeError(
                    f"Falha ao criar SHP de entrada (passo 2/final): {in_shp}. "
                    f"Arquivos em C:\\chasm: {listing}"
                )

            self._log(f"sDNA: INPUT final para provider = '{in_shp}'", Qgis.Info)

            # --- 4) Prepara parâmetro de saída (caminho explícito em disco) ---
            output_param_key = output_key or "OUTPUT"
            if alg is not None:
                try:
                    param_names = [p.name() for p in alg.parameterDefinitions()]
                except Exception:
                    param_names = []
                # garante que o nome bate com o algoritmo
                if output_param_key not in param_names:
                    if "OUTPUT" in param_names:
                        output_param_key = "OUTPUT"
                    elif "output" in param_names:
                        output_param_key = "output"

            out_shp = os.path.join(base_dir, f"out_{dw_field}_{uid}.shp")

            self._log(
                f"sDNA: usando parâmetro de saída '{output_param_key}' -> '{out_shp}'.",
                Qgis.Info
            )

            # --- 5) Monta parâmetros pro provider sDNA ---
            params = {
                input_key:        in_shp,          # caminho do SHP
                destw_key:        safe_dw,         # campo DW seguro
                output_param_key: out_shp,         # caminho explícito da saída
            }

            # Métrica / raios / modo
            if analmet_key is not None:
                params[analmet_key] = analmet_idx
            if radii_key is not None:
                params[radii_key] = radii_str
            if bandedradii_key is not None:
                params[bandedradii_key] = banded_val
            if cont_key is not None:
                params[cont_key] = cont_val

            # Betweenness / bidir
            if bet_key is not None:
                params[bet_key] = bet_val
            if bet_bi_key is not None:
                params[bet_bi_key] = bet_bi_val if bet_val else False

            # Weighting
            if weighting_key is not None and weighting_val is not None:
                params[weighting_key] = weighting_val

            # Pesos de origem / métrica custom
            if origweight_key is not None:
                params[origweight_key] = origin_weight_val or ""
            if custommetric_key is not None:
                params[custommetric_key] = (
                    custom_metric_field if (metric_val_str == "CUSTOM" and custom_metric_field) else ""
                )

            # Radial metric
            if radmet_key is not None:
                params[radmet_key] = defaults_map.get(radmet_key, 0)

            # Flags opcionais
            for key in (junctions_key, hull_key):
                if key is not None:
                    params[key] = False
            for key in (start_gs_key, end_gs_key, zonefiles_key, odfile_key,
                        disable_key, oneway_key, intermediates_key, advanced_key):
                if key is not None:
                    params[key] = ""

            # --- 6) Executa via Processing (provider sDNA) ---
            fb = ProcessingFeedback(self._log, prefix=f"sDNA[{dw_field}]")
            self._log(
                f"sDNA: chamando provider '{alg_id}' para DW='{dw_field}'\n"
                f"   INPUT = {in_shp}\n"
                f"   OUTPUT = {out_shp}",
                Qgis.Info, True
            )

            result = pr.run(alg_id, params, feedback=fb)
            self._log(f"sDNA[{dw_field}] resultado bruto: {result!r}", Qgis.Info)

            # --- 7) Tenta descobrir o que veio em 'OUTPUT' ---
            out_obj = None
            for key_try in ("OUTPUT", "output", output_param_key, output_key):
                if key_try and key_try in result and result[key_try]:
                    out_obj = result[key_try]
                    break

            out_name = f"{base_line_layer.name()}_sDNA_{dw_field}"
            out_path = None

            # Caso 1: já veio um QgsVectorLayer
            if isinstance(out_obj, QgsVectorLayer):
                out_lyr = out_obj
                out_lyr.setName(out_name)
                if not out_lyr.isValid():
                    raise RuntimeError("Camada retornada pelo provider sDNA está inválida.")
                return out_lyr

            # Caso 2: veio string (caminho)
            if isinstance(out_obj, str):
                out_path = out_obj.strip()
                self._log(f"sDNA[{dw_field}] OUTPUT (string) = '{out_path}'", Qgis.Info)
                if not out_path:
                    raise RuntimeError("Provider sDNA retornou OUTPUT vazio (string vazia).")

            # Caso 3: objeto estranho (ex.: ShapefileParameterVectorDestination)
            if out_obj is not None and out_path is None:
                self._log(
                    f"sDNA[{dw_field}] OUTPUT em objeto '{type(out_obj).__name__}': {out_obj!r}",
                    Qgis.Info, True
                )
                for attr in ("destination", "sink", "path", "filePath"):
                    try:
                        v = getattr(out_obj, attr, None)
                    except Exception:
                        v = None
                    if isinstance(v, str) and v.strip():
                        out_path = v.strip()
                        break

                if out_path:
                    self._log(f"sDNA[{dw_field}] OUTPUT (via atributo) = '{out_path}'", Qgis.Info)

            # Fallback: usa caminho explícito
            if (not out_path) and os.path.exists(out_shp):
                out_path = out_shp
                self._log(f"sDNA[{dw_field}] OUTPUT: usando caminho forçado '{out_path}'", Qgis.Info)

            if not out_path:
                raise RuntimeError("Provider sDNA terminou sem retornar OUTPUT utilizável.")

            out_lyr = QgsVectorLayer(out_path, out_name, "ogr")
            if not out_lyr or not out_lyr.isValid():
                raise RuntimeError(
                    f"Arquivo gerado pelo provider sDNA está inválido ao carregar: '{out_path}'."
                )
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
                if f.name().upper().startswith("MAD"):
                    mad_field = f.name()
                    break
            if not mad_field:
                raise RuntimeError(f"Campo MAD* não encontrado na saída do sDNA para DW '{dw}'.")

            # 2b — JOIN MAD (equals -> fallback intersects)
            try:
                joined = pr.run("native:joinattributesbylocation", {
                    "INPUT": current,
                    "JOIN": out_lyr,
                    "PREDICATE": [5],  # equals
                    "JOIN_FIELDS": [mad_field],
                    "METHOD": 0,
                    "DISCARD_NONMATCHING": False,
                    "PREFIX": "",
                    "OUTPUT": "TEMPORARY_OUTPUT"
                })["OUTPUT"]
            except Exception:
                joined = pr.run("native:joinattributesbylocation", {
                    "INPUT": current,
                    "JOIN": out_lyr,
                    "PREDICATE": [0],  # intersects
                    "JOIN_FIELDS": [mad_field],
                    "METHOD": 0,
                    "DISCARD_NONMATCHING": False,
                    "PREFIX": "",
                    "OUTPUT": "TEMPORARY_OUTPUT"
                })["OUTPUT"]

            target_name = dst_map.get(
                dw,
                f"mad_{dw}".replace(" ", "_").lower()[:30]
            )

            with_new = pr.run("native:fieldcalculator", {
                "INPUT": joined,
                "FIELD_NAME": target_name,
                "FIELD_TYPE": 0,
                "FIELD_LENGTH": 20,
                "FIELD_PRECISION": 6,
                "FORMULA": f"\"{mad_field}\"",
                "OUTPUT": "TEMPORARY_OUTPUT"
            })["OUTPUT"]
            cleaned = pr.run("native:deletecolumn", {
                "INPUT": with_new,
                "COLUMN": [mad_field],
                "OUTPUT": "TEMPORARY_OUTPUT"
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
            sdna_params = self.dlg.sdna_params() if self.dlg else {}
                
            self._log("OK/Final: iniciando Etapa 2 (sDNA + JOIN MAD)...", Qgis.Info, True)
            enriched = self._sdna_integral_and_join_mad(out, sdna_ui_params=sdna_params)
            self._log(
                f"OK/Final: Etapa 2 concluída -> '{enriched.name()}' com campos MAD.",
                Qgis.Success, True
            )
            self._msg(f"Processo concluído! Camada '{enriched.name()}' adicionada ao projeto.", Qgis.Success, 10)

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
            sdna_params = self.dlg.sdna_params() if self.dlg is not None else {}

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
            sdna_params = self.dlg.sdna_params() if self.dlg else {}

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
