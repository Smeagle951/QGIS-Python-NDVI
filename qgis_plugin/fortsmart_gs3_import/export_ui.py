"""FortSmart — dialogo de EXPORTACAO QGIS → monitores de maquinas."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from time import perf_counter

from qgis.PyQt.QtCore import QSignalBlocker, Qt
from qgis.PyQt.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QProgressDialog,
    QRadioButton,
    QTextEdit,
    QVBoxLayout,
)
from qgis.core import Qgis, QgsMessageLog, QgsProject, QgsVectorLayer

from .canonical_project import CanonicalProjectBuilder
from .dialog_scroll import build_scroll_dialog, fit_dialog_to_screen
from .export_collector import apply_monitor_hierarchy, collect_export_bundles, summarize_text
from .export_destination_ui import MONITOR_FOLDER_PREVIEW, ExportDestinationPanel
from .field_group_export import choose_export_fields, export_name_map, group_export_layers
from .jd_guidance_plan import (
    BORDER_PASS,
    FINAL_WORK_PATH,
    GEN4,
    GROUPING_PER_CATEGORY,
    GROUPING_PER_PASS,
    GS3,
    GS3_PER_PASS_STATUS,
    PASS_CATEGORIES,
    PASS_CATEGORY_BY_ROLE,
    PLANTING_PASS,
    PASS_NAME_PT,
    PASS_OPTION_LABEL,
    JohnDeereGuidanceOptions,
    detected_pass_counts,
    final_lines_source_lines,
    format_layer_audit,
    format_preview_counts,
    guidance_block_reasons,
    guidance_enabled,
    guidance_inclusion_lines,
    john_deere_preview_counts,
    layer_not_collected_errors,
    multiple_external_boundary_errors,
    pass_layer_audit,
    source_pass_counts,
    unrequested_passes,
)
from .jd_guidance_report import read_guidance_report, success_summary
from .monitor_registry import export_fields, get_profile, list_export_profiles
from .sendto2020_traffic_exclusions import (
    BOUNDARY_ONLY,
    BOUNDARY_WITH_TRAFFIC_EXCLUSIONS,
    TRAFFIC_REPORT_JSON,
    TRAFFIC_REPORT_TEXT,
    TrafficExclusionError,
    planner_traffic_sources,
    traffic_success_lines,
)
from .usb_copy import copy_payload_to_usb, list_usb_drives, verify_copy


OUTPUT_ROOT = Path(__file__).resolve().parent.parent.parent / "data" / "output"
JOHN_DEERE_PROFILES = {
    "john_deere_gs3": (GS3,),
    "john_deere_gen4": (GEN4,),
    "john_deere_dual": (GS3, GEN4),
}
GS3_PASSES_ENABLED = any(guidance_enabled(GS3, category) for category in PASS_CATEGORIES)
GEN4_PASSES_ENABLED = any(guidance_enabled(GEN4, category) for category in PASS_CATEGORIES)


def requested_pass_roles(options: JohnDeereGuidanceOptions | None, platforms) -> set[str]:
    if options is None:
        return set()
    roles = {
        role
        for role, category in PASS_CATEGORY_BY_ROLE.items()
        if any(options.platform_requested(platform, category) for platform in platforms)
    }
    if "WORK_PATH" in roles:
        roles.add(FINAL_WORK_PATH)
    return roles


def pass_layer_ids(layers, roles: set[str]) -> set[str]:
    """Camadas de passada pela fortsmart_role, mesmo ocultas no painel."""
    if not roles:
        return set()
    builder = CanonicalProjectBuilder()
    ids = set()
    for layer in layers:
        ref = builder.resolve_layer(layer)
        if ref is not None and ref.role in roles:
            ids.add(layer.id())
    return ids


def john_deere_preview_text(bundles, audit: list[dict], options, platforms) -> str:
    lines = ["", "John Deere — plano de exportação:"]
    lines.extend(format_preview_counts(john_deere_preview_counts(bundles)))
    lines.extend(guidance_inclusion_lines(detected_pass_counts(bundles, audit), options, platforms))
    lines.extend(final_lines_source_lines(bundles))
    skipped = [entry for entry in pass_layer_audit(audit) if not entry.get("collected")]
    if skipped:
        lines.append("Camadas de passada fora do plano:")
        lines.extend(f"  {format_layer_audit(entry)}" for entry in skipped)
    reasons = []
    if options is not None:
        options = replace(options, source_pass_counts=source_pass_counts(audit))
        requested = [
            category for category in PASS_CATEGORIES
            if any(options.platform_requested(platform, category) for platform in platforms)
        ]
        reasons = layer_not_collected_errors(audit, requested)
    reasons += guidance_block_reasons(john_deere_preview_counts(bundles), options, platforms)
    if reasons:
        lines.append("EXPORTAÇÃO JOHN DEERE COM PASSADAS BLOQUEADA:")
        lines.extend(f"  {reason}" for reason in reasons)
    boundary_errors = multiple_external_boundary_errors(bundles)
    if boundary_errors:
        lines.append("EXPORTAÇÃO JOHN DEERE BLOQUEADA:")
        lines.extend(f"  {error}" for error in boundary_errors)
    return "\n".join(lines)


PAYLOAD_BY_PROFILE = {
    "john_deere_dual": "Gen4/ + GS3_2630/{Fazenda}/RCD/EIC/",
    "john_deere_gs3": "GS3_2630/{Fazenda}/RCD/EIC/ (fdShape) → Import Data no GS3 2630",
    "john_deere_gen4": "Gen4/MasterData.xml + SpatialFiles/*.gjson",
    "isoxml_v4": "TASKDATA/TASKDATA.XML + LINKLIST.XML (ISO 11783-10 v4)",
    "isoxml_v3": "TASKDATA/TASKDATA.XML (ISO v3)",
    "cnh": "TASKDATA/ (ISOXML) para AFS Pro / PLM",
    "fendt": "TASKDATA/ (ISOXML) para Variotronic",
    "jacto": "OMNI/Data/{Fazenda}/{Talhao}/_perimeter + _runline",
    "sendto2020": (
        "SendTo2020/{Talhao}.shp + .mcd — importa o arquivo de limite; "
        "o talhao deve ser criado/selecionado e o boundary associado no 20|20"
    ),
    "stara": "DADOS/TALHOES + DADOS/GUIAS",
    "precision_planting": "TASKDATA/ (ISOXML)",
}


class FortSmartExportDialog(QDialog):
    """Modelo de exportacao: trabalhos do QGIS prontos para o monitor."""

    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.setWindowTitle("FortSmart — Exportar trabalhos para o monitor")
        fit_dialog_to_screen(self, width=520, height=500, min_width=460, min_height=360)

        title = QLabel("<b>QGIS → Monitor</b> — exporta camadas visiveis para o formato do monitor.")
        title.setWordWrap(True)
        title.setTextFormat(Qt.RichText)

        # --- Monitor ---
        mon_box = QGroupBox("1. Monitor destino")
        self.monitor_combo = QComboBox()
        for profile in list_export_profiles():
            self.monitor_combo.addItem(f"{profile.brand} — {profile.name}", profile.id)
        self.payload_label = QLabel("")
        self.payload_label.setWordWrap(True)
        mon_form = QFormLayout(mon_box)
        mon_form.addRow("Destino", self.monitor_combo)
        mon_form.addRow("Formato gerado", self.payload_label)

        # --- Hierarquia do monitor ---
        hierarchy_box = QGroupBox("2. Hierarquia no monitor")
        self.owner_edit = QLineEdit()
        self.owner_edit.setPlaceholderText("Nome do proprietario")
        self.farm_edit = QLineEdit()
        self.farm_edit.setPlaceholderText("Nome da fazenda")
        hierarchy_hint = QLabel(
            "Proprietario → Fazenda → Talhoes dos grupos QGIS. "
            "Antes de exportar, selecione os grupos e confira os nomes reais."
        )
        hierarchy_hint.setWordWrap(True)
        hierarchy_form = QFormLayout(hierarchy_box)
        hierarchy_form.addRow("Proprietario", self.owner_edit)
        hierarchy_form.addRow("Fazenda", self.farm_edit)
        hierarchy_form.addRow("Ordem", hierarchy_hint)

        # --- Conteudo QGIS ---
        src_box = QGroupBox("3. Camadas QGIS")
        self.include_loose = QCheckBox("Incluir AdaptiveCurve, Limite, LINHAS…")
        self.include_loose.setChecked(True)
        self.only_visible = QCheckBox("Apenas visiveis no painel")
        self.only_visible.setChecked(True)
        refresh_btn = QPushButton("Atualizar preview")
        refresh_btn.clicked.connect(self._refresh_preview)
        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setMaximumHeight(190)
        self.preview.setMinimumHeight(120)
        src_layout = QVBoxLayout(src_box)
        src_layout.addWidget(self.include_loose)
        src_layout.addWidget(self.only_visible)
        src_layout.addWidget(refresh_btn)
        src_layout.addWidget(self.preview)

        # --- Saida ---
        self.destination = ExportDestinationPanel(
            title="4. Onde salvar",
            default_parent=OUTPUT_ROOT,
            default_folder_name="export_monitor",
        )
        self.destination.path_changed.connect(self._refresh_destination_tree)

        self.template_edit = QLineEdit("E:/GS3_2630")
        browse_tpl = QPushButton("Pasta…")
        browse_tpl.clicked.connect(self._browse_template)
        tpl_row = QHBoxLayout()
        tpl_row.addWidget(self.template_edit, 1)
        tpl_row.addWidget(browse_tpl)
        self.use_template = QCheckBox("GS3: usar template do pendrive (host + setup.fds)")
        self.use_template.setChecked(True)
        self.template_label = QLabel("Template GS3")

        tpl_box = QGroupBox("Template GS3")
        self.tpl_box = tpl_box
        tpl_form = QFormLayout(tpl_box)
        tpl_form.addRow("", self.use_template)
        tpl_form.addRow(self.template_label, tpl_row)

        # Desmarcado por padrao ate DEVICE_VERIFIED: sem marcar, os pacotes GS3/Gen4 nao mudam.
        self.passes_box = QGroupBox("JOHN DEERE — GUIDANCE")
        self.export_planting = QCheckBox("Exportar passadas de plantio")
        self.export_planting.setChecked(False)
        self.export_border = QCheckBox("Exportar passadas de bordadura")
        self.export_border.setChecked(False)
        self.dest_gs3 = QCheckBox("GS3 / 2630")
        self.dest_gs3.setChecked(True)
        self.dest_gen4 = QCheckBox("Gen4 / Universal")
        self.dest_gen4.setChecked(True)
        dest_row = QHBoxLayout()
        dest_row.addWidget(self.dest_gs3)
        dest_row.addWidget(self.dest_gen4)
        dest_row.addStretch(1)
        self.pass_limit = QComboBox()
        self.pass_limit.addItem("Todas", 0)
        self.pass_limit.addItem("5", 5)
        self.pass_limit.addItem("20", 20)
        self.pass_limit.setToolTip("Limite por talhao para o teste no monitor: 5, depois 20, depois todas.")
        self.gs3_per_pass = QCheckBox(f"GS3: uma linha GS3 por passada ({GS3_PER_PASS_STATUS})")
        self.gs3_per_pass.setChecked(False)
        passes_hint = QLabel(
            "Status: READY_FOR_DEVICE_TEST. Teste no monitor: GS3 com 5, 20 e todas, "
            "bordadura separada; depois Gen4 com 5 e todas."
        )
        passes_hint.setWordWrap(True)
        passes_form = QFormLayout(self.passes_box)
        passes_form.addRow("", self.export_planting)
        passes_form.addRow("", self.export_border)
        passes_form.addRow("Destino", dest_row)
        passes_form.addRow("Limite (teste)", self.pass_limit)
        passes_form.addRow("", self.gs3_per_pass)
        passes_form.addRow("", passes_hint)

        self.pp_reference_edit = QLineEdit()
        self.pp_reference_edit.setPlaceholderText("Pasta SendTo2020 com talhoes ja existentes no 20|20")
        browse_pp = QPushButton("Pasta…")
        browse_pp.clicked.connect(self._browse_pp_reference)
        pp_row = QHBoxLayout()
        pp_row.addWidget(self.pp_reference_edit, 1)
        pp_row.addWidget(browse_pp)
        pp_hint = QLabel(
            "O 20|20 agrupa pelo nome exato de Business e Farm. Informe um SendTo2020 "
            "ja aceito pelo monitor (ou deixe o pendrive conectado) para usar os mesmos nomes."
        )
        pp_hint.setWordWrap(True)
        self.pp_box = QGroupBox("Talhoes existentes no 20|20")
        pp_form = QFormLayout(self.pp_box)
        pp_form.addRow("Referencia", pp_row)
        pp_form.addRow("", pp_hint)
        self.pp_mode_normal = QRadioButton("Boundary normal")
        self.pp_mode_normal.setChecked(True)
        self.pp_mode_traffic = QRadioButton("Boundary + exclusoes dos rastros")
        self.pp_mode_traffic.setToolTip(
            "Usa o resultado operacional ACTIVE salvo no FortSmart Precision: cada rastro de pneu "
            "do pulverizador vira um buraco interno no limite. Exclusoes que tocam ou cruzam o "
            "limite bloqueiam a exportacao. Validacao fisica no 20|20 pendente."
        )
        pp_mode_row = QVBoxLayout()
        pp_mode_row.addWidget(self.pp_mode_normal)
        pp_mode_row.addWidget(self.pp_mode_traffic)
        pp_form.addRow("Modo do boundary", pp_mode_row)

        # --- USB opcional ---
        usb_box = QGroupBox("5. Copiar USB")
        self.copy_usb = QCheckBox("Copiar para pendrive apos exportar")
        self.copy_usb.setChecked(False)
        self.usb_combo = QComboBox()
        refresh_usb = QPushButton("Atualizar")
        refresh_usb.clicked.connect(self._refresh_usbs)
        usb_row = QHBoxLayout()
        usb_row.addWidget(self.usb_combo, 1)
        usb_row.addWidget(refresh_usb)
        self.require_fat32 = QCheckBox("Exigir FAT32 (GS3)")
        self.require_fat32.setChecked(True)
        usb_layout = QVBoxLayout(usb_box)
        usb_layout.addWidget(self.copy_usb)
        usb_layout.addLayout(usb_row)
        usb_layout.addWidget(self.require_fat32)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Exportar")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        content_layout = build_scroll_dialog(self, buttons)
        content_layout.addWidget(title)
        content_layout.addWidget(mon_box)
        content_layout.addWidget(hierarchy_box)
        content_layout.addWidget(src_box)
        content_layout.addWidget(self.passes_box)
        content_layout.addWidget(self.destination)
        content_layout.addWidget(self.tpl_box)
        content_layout.addWidget(self.pp_box)
        content_layout.addWidget(usb_box)

        self.monitor_combo.currentIndexChanged.connect(self._on_monitor_changed)
        self.include_loose.stateChanged.connect(self._refresh_preview)
        self.only_visible.stateChanged.connect(self._refresh_preview)
        for box in (self.export_planting, self.export_border, self.dest_gs3, self.dest_gen4):
            box.stateChanged.connect(self._refresh_preview)
        # Text input must not rescan a large QGIS project on every keystroke.
        # The preview button performs the explicit refresh when needed.
        self._on_monitor_changed()
        self._refresh_destination_tree()
        self._refresh_usbs()
        self._refresh_preview()

    def _on_monitor_changed(self):
        pid = self.monitor_combo.currentData()
        self.payload_label.setText(PAYLOAD_BY_PROFILE.get(pid, "Pasta no formato do monitor"))
        needs_template = pid in ("john_deere_gs3", "john_deere_dual")
        self.tpl_box.setVisible(needs_template)
        self.use_template.setVisible(needs_template)
        self.template_label.setVisible(needs_template)
        self.template_edit.setVisible(needs_template)
        self.template_edit.setEnabled(needs_template)
        platforms = JOHN_DEERE_PROFILES.get(pid, ())
        gs3_on = GS3 in platforms and GS3_PASSES_ENABLED
        gen4_on = GEN4 in platforms and GEN4_PASSES_ENABLED
        self.passes_box.setVisible(gs3_on or gen4_on)
        self.dest_gs3.setVisible(gs3_on)
        self.dest_gen4.setVisible(gen4_on)
        self.gs3_per_pass.setVisible(gs3_on)
        self.export_planting.setVisible(
            (gs3_on and guidance_enabled(GS3, PLANTING_PASS)) or (gen4_on and guidance_enabled(GEN4, PLANTING_PASS))
        )
        self.export_border.setVisible(
            (gs3_on and guidance_enabled(GS3, BORDER_PASS)) or (gen4_on and guidance_enabled(GEN4, BORDER_PASS))
        )
        self.pp_box.setVisible(pid == "sendto2020")
        profile = get_profile(pid)
        if profile.notes:
            self.payload_label.setText(self.payload_label.text() + f"\n{profile.notes}")
        self._refresh_destination_tree()
        self._refresh_preview()

    def _refresh_destination_tree(self) -> None:
        pid = self.monitor_combo.currentData()
        lines = MONITOR_FOLDER_PREVIEW.get(pid, ["(formato do monitor selecionado)"])
        self.destination.set_tree_lines(lines)

    def _browse_template(self):
        path = QFileDialog.getExistingDirectory(self, "Template GS3_2630")
        if path:
            self.template_edit.setText(path)

    def _browse_pp_reference(self):
        path = QFileDialog.getExistingDirectory(self, "SendTo2020 existente do 20|20")
        if path:
            self.pp_reference_edit.setText(path)

    def _refresh_usbs(self):
        self.usb_combo.clear()
        drives = list_usb_drives()
        if not drives:
            self.usb_combo.addItem("(nenhuma unidade)", "")
            return
        for d in drives:
            mark = " FAT32" if d.fat32_ok else f" {d.file_system}"
            self.usb_combo.addItem(f"{d.letter}:{mark} {d.label}".strip(), d.letter)

    def _requested_pass_roles(self) -> set[str]:
        platforms = JOHN_DEERE_PROFILES.get(self.monitor_combo.currentData(), ())
        return requested_pass_roles(self._guidance_options(), platforms)

    def _selected_visible_ids(self) -> set[str] | None:
        if not self.only_visible.isChecked():
            return None
        ids: set[str] = set()
        root = QgsProject.instance().layerTreeRoot()
        for layer in QgsProject.instance().mapLayers().values():
            node = root.findLayer(layer.id())
            if node and node.isVisible():
                ids.add(layer.id())
        vector_layers = [
            layer for layer in QgsProject.instance().mapLayers().values() if isinstance(layer, QgsVectorLayer)
        ]
        return ids | pass_layer_ids(vector_layers, self._requested_pass_roles())

    def _collect(self, audit: list[dict] | None = None):
        selected = self._selected_visible_ids()
        project = QgsProject.instance()
        candidates = [
            layer for layer in project.mapLayers().values()
            if isinstance(layer, QgsVectorLayer) and (selected is None or layer.id() in selected)
        ]
        # Mesmo agrupamento por talhao da exportacao (grupo do painel de camadas).
        field_names = {
            layer_id: group.label
            for group in group_export_layers(candidates, project.layerTreeRoot())
            for layer_id in group.layer_ids
        }
        return collect_export_bundles(
            only_selected_layers=selected is not None,
            selected_layer_ids=selected,
            include_loose_layers=self.include_loose.isChecked(),
            field_names_by_layer_id=field_names,
            audit=audit,
        )

    def _refresh_preview(self):
        try:
            audit: list[dict] = []
            bundles = self._collect(audit)
            if bundles:
                if not self.owner_edit.text().strip() and bundles[0].cliente not in ("Cliente", "Proprietario"):
                    with QSignalBlocker(self.owner_edit):
                        self.owner_edit.setText(bundles[0].cliente)
                if not self.farm_edit.text().strip() and bundles[0].fazenda != "Fazenda":
                    with QSignalBlocker(self.farm_edit):
                        self.farm_edit.setText(bundles[0].fazenda)
                owner = self.owner_edit.text().strip() or bundles[0].cliente
                farm = self.farm_edit.text().strip() or bundles[0].fazenda
                bundles = apply_monitor_hierarchy(bundles, proprietario=owner, fazenda=farm)
            pid = self.monitor_combo.currentData()
            profile = get_profile(pid)
            text = summarize_text(bundles, profile.name)
            platforms = JOHN_DEERE_PROFILES.get(pid, ())
            if platforms:
                text += "\n" + john_deere_preview_text(bundles, audit, self._guidance_options(), platforms)
            self.preview.setPlainText(text)
        except Exception as exc:
            self.preview.setPlainText(f"Erro ao ler camadas: {exc}")

    def accept(self):
        if self._confirm_guidance():
            super().accept()

    def _confirm_guidance(self) -> bool:
        """Passadas no projeto com a opcao desmarcada: perguntar, nunca omitir em silencio."""
        platforms = JOHN_DEERE_PROFILES.get(self.monitor_combo.currentData(), ())
        if not platforms:
            return True
        try:
            audit: list[dict] = []
            counts = detected_pass_counts(self._collect(audit), audit)
        except Exception:
            return True
        missing = unrequested_passes(counts, self._guidance_options(), platforms)
        if not missing:
            return True
        found = " e ".join(f"{total} {PASS_NAME_PT[category]}" for category, total in missing)
        options = ", ".join(f"'{PASS_OPTION_LABEL[category]}'" for category, _ in missing)
        answer = QMessageBox.question(
            self,
            "FortSmart — linhas de guia John Deere",
            f"O projeto tem {found}, mas {options} está desmarcado.\n\n"
            "Sem marcar, o pacote John Deere sai só com limites e bordadura, "
            "sem as linhas de guia.\n\n"
            "Sim: incluir as passadas no pacote\n"
            "Não: exportar sem as passadas\n"
            "Cancelar: voltar ao diálogo",
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            QMessageBox.Yes,
        )
        if answer == QMessageBox.Cancel:
            return False
        if answer == QMessageBox.Yes:
            for category, _ in missing:
                (self.export_planting if category == PLANTING_PASS else self.export_border).setChecked(True)
            if GS3 in platforms and not self.dest_gs3.isChecked() and not self.dest_gen4.isChecked():
                self.dest_gs3.setChecked(True)
            if GEN4 in platforms and not self.dest_gen4.isChecked() and not self.dest_gs3.isChecked():
                self.dest_gen4.setChecked(True)
        return True

    def values(self) -> dict:
        return {
            "profile_id": self.monitor_combo.currentData(),
            "output_root": self.destination.full_path(),
            "template_gs3": Path(self.template_edit.text().strip()) if self.use_template.isChecked() else None,
            "merge_setup": self.use_template.isChecked(),
            "copy_usb": self.copy_usb.isChecked(),
            "usb_letter": self.usb_combo.currentData(),
            "require_fat32": self.require_fat32.isChecked(),
            "include_loose": self.include_loose.isChecked(),
            "only_visible": self.only_visible.isChecked(),
            "proprietario": self.owner_edit.text().strip(),
            "fazenda": self.farm_edit.text().strip(),
            "sendto2020_reference": (
                Path(self.pp_reference_edit.text().strip())
                if self.pp_reference_edit.text().strip()
                else None
            ),
            "guidance_options": self._guidance_options(),
            "sendto2020_boundary_mode": (
                BOUNDARY_WITH_TRAFFIC_EXCLUSIONS if self.pp_mode_traffic.isChecked() else BOUNDARY_ONLY
            ),
        }

    def _guidance_options(self) -> JohnDeereGuidanceOptions | None:
        platforms = JOHN_DEERE_PROFILES.get(self.monitor_combo.currentData(), ())
        if not platforms:
            return None
        limit = int(self.pass_limit.currentData() or 0)
        options = JohnDeereGuidanceOptions(
            include_planting_passes=self.export_planting.isChecked(),
            include_border_passes=self.export_border.isChecked(),
            export_gs3=GS3 in platforms and self.dest_gs3.isChecked(),
            export_gen4=GEN4 in platforms and self.dest_gen4.isChecked(),
            grouping=GROUPING_PER_PASS if self.gs3_per_pass.isChecked() else GROUPING_PER_CATEGORY,
            max_planting_passes=limit,
            max_border_passes=limit,
        )
        if not any(options.any_platform_requested(platform) for platform in platforms):
            return None
        return options


def _guidance_summary(output_root: Path) -> str:
    report = read_guidance_report(output_root)
    if report is None:
        return "\n\nPacote incompleto: foram solicitadas linhas de guia, mas o relatorio esta ausente."
    lines = success_summary(report)
    for field in report.get("preflight", {}).get("fields", []):
        lines.extend(f"Aviso: {warning}" for warning in field.get("warnings", [])[:5])
    lines.append("Confirme no monitor antes de usar em operacao (GS3_DEVICE_VERIFIED / GEN4_DEVICE_VERIFIED).")
    return "\n\n" + "\n".join(lines)


def run_export(iface) -> Path:
    dialog = FortSmartExportDialog(iface)
    if dialog.exec_() != QDialog.Accepted:
        raise RuntimeError("Export cancelado.")

    values = dialog.values()
    profile_id = values["profile_id"]

    project = QgsProject.instance()
    root = project.layerTreeRoot()
    vector_layers = [layer for layer in project.mapLayers().values() if isinstance(layer, QgsVectorLayer)]
    platforms = JOHN_DEERE_PROFILES.get(profile_id, ())
    forced_pass_ids = pass_layer_ids(vector_layers, requested_pass_roles(values["guidance_options"], platforms))
    candidates = [
        layer for layer in vector_layers
        if not values["only_visible"]
        or (root.findLayer(layer.id()) and root.findLayer(layer.id()).isVisible())
        or layer.id() in forced_pass_ids
    ]
    selection = choose_export_fields(iface.mainWindow(), candidates, root)
    if selection is None:
        raise RuntimeError("Export cancelado.")
    field_groups, edited_names = selection
    field_names = export_name_map(field_groups, edited_names)

    progress = QProgressDialog("Preparando exportacao...", "", 0, 4, iface.mainWindow())
    progress.setWindowTitle("FortSmart - Exportacao")
    progress.setCancelButton(None)
    progress.setWindowModality(Qt.WindowModal)
    progress.setMinimumDuration(0)
    progress.show()

    started = perf_counter()

    def phase(step: int, message: str) -> None:
        progress.setValue(step)
        progress.setLabelText(message)
        iface.statusBarIface().showMessage(message)
        QgsMessageLog.logMessage(message, "FortSmart Export", Qgis.Info)
        QApplication.processEvents()

    try:
        phase(0, "FortSmart: coletando camadas visiveis...")
        audit: list[dict] = []
        bundles = collect_export_bundles(
            only_selected_layers=True,
            selected_layer_ids=set(field_names),
            include_loose_layers=values["include_loose"],
            field_names_by_layer_id=field_names,
            audit=audit,
        )
        for entry in audit:
            if entry.get("fortsmart_role") or entry.get("collected"):
                QgsMessageLog.logMessage(format_layer_audit(entry), "FortSmart Export", Qgis.Info)
        if values["guidance_options"] is not None:
            values["guidance_options"] = replace(
                values["guidance_options"],
                source_pass_counts=source_pass_counts(audit),
                collection_audit=tuple(pass_layer_audit(audit)),
            )
        if not bundles:
            raise RuntimeError(
                "Nenhum trabalho encontrado no projeto QGIS.\n\n"
                "Opcoes:\n"
                "• Importe com 'Organizar por talhao', OU\n"
                "• Crie/edite camadas com atributos: proprietario, fazenda, talhao, tipo\n"
                "  (limite, linha_ab, curva_ab, bordadura, marcacao...)"
            )

        phase(1, f"FortSmart: organizando {len(bundles)} talhoes...")
        proprietario = values["proprietario"] or bundles[0].cliente
        fazenda = values["fazenda"] or bundles[0].fazenda
        if proprietario in ("", "Cliente", "Proprietario"):
            raise RuntimeError("Informe o nome do proprietario na hierarquia do monitor.")
        if fazenda in ("", "Fazenda"):
            raise RuntimeError("Informe o nome da fazenda na hierarquia do monitor.")
        bundles = apply_monitor_hierarchy(
            bundles,
            proprietario=proprietario,
            fazenda=fazenda,
        )

        phase(2, f"FortSmart: gerando pacote {get_profile(profile_id).name}...")
        traffic_mode = (
            profile_id == "sendto2020"
            and values["sendto2020_boundary_mode"] == BOUNDARY_WITH_TRAFFIC_EXCLUSIONS
        )
        if traffic_mode:
            from .adapters.sendto2020_writer import SendTo2020Writer

            field_labels = {
                " ".join(edited_names.get(group.key, "").split()): group.label for group in field_groups
            }
            try:
                options = {
                    "sendto2020_boundary_mode": BOUNDARY_WITH_TRAFFIC_EXCLUSIONS,
                    "sendto2020_traffic_sources": planner_traffic_sources(project.absolutePath(), field_labels),
                }
                if values["sendto2020_reference"]:
                    options["sendto2020_reference"] = values["sendto2020_reference"]
                out = SendTo2020Writer().write(bundles, values["output_root"], **options)
            except TrafficExclusionError as exc:
                raise RuntimeError(
                    "Boundary + exclusoes dos rastros bloqueado. Nenhum pacote novo foi publicado.\n\n"
                    f"{exc}\n\n"
                    f"Diagnostico: {Path(values['output_root']) / 'SENDTO2020_DIAGNOSTIC_EXPORT' / TRAFFIC_REPORT_TEXT}\n"
                    "O modo 'Boundary normal' continua disponivel."
                ) from exc
        else:
            out = export_fields(
                bundles,
                values["output_root"],
                profile_id=profile_id,
                template_gs3=values["template_gs3"],
                merge_setup=values["merge_setup"],
                sendto2020_reference=values["sendto2020_reference"],
                guidance_options=values["guidance_options"],
            )

        msg = (
            f"Export concluido.\n\n"
            f"Monitor: {get_profile(profile_id).name}\n"
            f"Proprietario: {proprietario}\n"
            f"Fazenda: {fazenda}\n"
            f"Talhoes: {len(bundles)}\n"
            f"Pasta: {out}\n\n"
            f"{PAYLOAD_BY_PROFILE.get(profile_id, '')}"
        )

        if values["guidance_options"] is not None:
            msg += _guidance_summary(Path(values["output_root"]))

        if profile_id == "sendto2020":
            boundary_names = ", ".join(path.stem for path in sorted(Path(out).glob("*.shp")))
            msg += (
                "\n\nIMPORTANTE — ETAPA OBRIGATORIA NO 20|20:\n"
                "1. Setup > Settings > Import Data > Field Boundaries.\n"
                "2. Importe o arquivo de limite.\n"
                "3. Va a Setup > Fields e crie/selecione Cliente, Fazenda e Talhao.\n"
                "4. Abra Field Setup > Field Boundary e associe o arquivo importado.\n"
                "5. Ative o talhao para mostrar o limite e configurar headland.\n"
                f"Boundary(s) para selecionar: {boundary_names or '(nenhum)'}\n\n"
                "O SHP de boundary nao cria a hierarquia de talhoes automaticamente."
            )
            report_path = Path(values["output_root"]) / "SENDTO2020_DIAGNOSTIC_EXPORT" / "SENDTO2020_EXPORT_REPORT.json"
            try:
                import json

                pp_report = json.loads(report_path.read_text(encoding="utf-8"))
                used = [
                    f"{f['business']} / {f['farm']} / {f['field']} ({f['export_area_ha']:.2f} ha)"
                    for f in pp_report.get("fields", [])
                ]
                msg += "\n\nNomes gravados para o 20|20:\n" + "\n".join(used)
                if pp_report.get("warnings"):
                    msg += "\n\nAvisos:\n" + "\n".join(pp_report["warnings"])
            except (OSError, ValueError, KeyError):
                pass
            try:
                import json

                from .precision2020_boundary import REPORT_JSON, precision2020_success_lines

                boundary_report = json.loads((report_path.parent / REPORT_JSON).read_text(encoding="utf-8"))
                msg += "\n\n" + "\n".join(precision2020_success_lines(boundary_report))
            except (OSError, ValueError, KeyError):
                pass
            if traffic_mode:
                try:
                    import json

                    traffic_report = json.loads(
                        (report_path.parent / TRAFFIC_REPORT_JSON).read_text(encoding="utf-8")
                    )
                    msg += "\n\n" + "\n".join(traffic_success_lines(traffic_report))
                except (OSError, ValueError, KeyError):
                    msg += "\n\nRelatorio das exclusoes de trafego ausente."

        if values["copy_usb"]:
            phase(3, "FortSmart: copiando e verificando o pendrive...")
            letter = values["usb_letter"]
            if not letter:
                QMessageBox.warning(iface.mainWindow(), "FortSmart Export", msg + "\n\nUSB: nenhuma unidade selecionada.")
                return out
            drives = {d.letter: d for d in list_usb_drives()}
            drive = drives.get(letter)
            if values["require_fat32"] and drive and not drive.fat32_ok:
                raise RuntimeError(
                    f"Unidade {letter}: esta em {drive.file_system}, nao FAT32.\n"
                    "Para GS3 use pendrive FAT32, ou desmarque 'Exigir FAT32'."
                )
            dest = copy_payload_to_usb(values["output_root"], Path(f"{letter}:\\"), overwrite=True)
            issues = verify_copy(dest)
            msg += f"\n\nCopiado para USB: {dest}"
            if issues:
                msg += "\nAvisos: " + "; ".join(issues)
            else:
                msg += "\nVerificacao USB: OK"

        elapsed = perf_counter() - started
        phase(4, f"FortSmart: exportacao concluida em {elapsed:.1f}s")
    finally:
        progress.close()
        iface.statusBarIface().clearMessage()

    QMessageBox.information(iface.mainWindow(), "FortSmart Export", msg)
    return out
